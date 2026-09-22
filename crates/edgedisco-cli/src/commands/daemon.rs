use crate::cli::{DaemonArgs, IpcModeArg};
use crate::config::SettingsManager;
use crate::ipc::{default_user_socket_path, DaemonIpcState, IpcConfig, IpcServer, ScanCommand};
use crate::util::{
    current_timestamp, default_database_path, get_hostname, get_machine, get_os_name,
    get_os_version, local_device_id, random_token_hash,
};
use edgedisco_core::exporter::{ExportError, OtlpExporter};
use edgedisco_core::models::Device;
use edgedisco_core::store::Store;
use std::collections::BTreeSet;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tokio::signal;
use tokio::sync::{mpsc, watch};

/// Ensure an enrolled local device record exists in the store, returning the device ID.
pub fn ensure_local_device(store: &Store) -> Result<String, Box<dyn std::error::Error>> {
    let hostname = get_hostname();
    let os = get_os_name();
    let machine = get_machine();
    let dev_id = local_device_id(&hostname, &os, machine.as_deref());
    if store.get_device(&dev_id)?.is_some() {
        return Ok(dev_id);
    }
    let now = current_timestamp();

    let device = Device::new(
        dev_id.clone(),
        hostname,
        os,
        get_os_version(),
        machine,
        Some(env!("CARGO_PKG_VERSION").to_string()),
        random_token_hash(),
        now.clone(),
        now,
    );

    store.enroll_device(&device)?;
    Ok(dev_id)
}

/// Run a single collection iteration: process scan, catalog classification, and SQLite upsert.
pub fn run_daemon_iteration(store: &Store) -> Result<usize, Box<dyn std::error::Error>> {
    let report = super::scan::generate_scan_report()?;
    super::scan::persist_scan_report_to_store(store, &report)
}

async fn export_outbox_once(store: &Store, exporter: &OtlpExporter) -> Result<(), ExportError> {
    let outcome = exporter.export_once_at(store, &current_timestamp()).await?;
    if outcome.claimed > 0 {
        eprintln!(
            "[{}] OTLP export: {} delivered, {} scheduled for retry, {} failed",
            current_timestamp(),
            outcome.delivered,
            outcome.retried,
            outcome.failed
        );
    }
    Ok(())
}

async fn export_outbox_if_configured(
    store: &Store,
    args: &DaemonArgs,
) -> Result<(), Box<dyn std::error::Error>> {
    if let Some(exporter) = &args.otlp_exporter {
        export_outbox_once(store, exporter).await?;
    }
    Ok(())
}

fn spawn_export_worker(
    store: Arc<Store>,
    mut settings: watch::Receiver<DaemonArgs>,
    mut shutdown: watch::Receiver<bool>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(1));
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            tokio::select! {
                _ = shutdown.changed() => break,
                _ = interval.tick() => {
                    let exporter = settings.borrow_and_update().otlp_exporter.as_deref().cloned();
                    let Some(exporter) = exporter else { continue };
                    let result = tokio::select! {
                        _ = shutdown.changed() => break,
                        result = export_outbox_once(&store, &exporter) => result,
                    };
                    if result.is_err() {
                        eprintln!("[{}] OTLP export error", current_timestamp());
                    }
                }
            }
        }
    })
}

fn ipc_config(args: &DaemonArgs) -> Result<IpcConfig, Box<dyn std::error::Error>> {
    match args.ipc_mode {
        IpcModeArg::User => {
            if !args.ipc_allowed_uid.is_empty()
                || !args.ipc_allowed_gid.is_empty()
                || args.ipc_owner_uid.is_some()
                || args.ipc_group_gid.is_some()
            {
                return Err("system IPC identity options require --ipc-mode system".into());
            }
            let path = args
                .ipc_socket
                .clone()
                .map(Ok)
                .unwrap_or_else(default_user_socket_path)?;
            Ok(IpcConfig::user(path, unsafe { libc::geteuid() }))
        }
        IpcModeArg::System => {
            let path = args
                .ipc_socket
                .clone()
                .unwrap_or_else(|| PathBuf::from("/var/run/edgedisco.sock"));
            let allowed_uids = args
                .ipc_allowed_uid
                .iter()
                .copied()
                .collect::<BTreeSet<_>>();
            let allowed_gids = args
                .ipc_allowed_gid
                .iter()
                .copied()
                .collect::<BTreeSet<_>>();
            let owner = args.ipc_owner_uid.zip(args.ipc_group_gid);
            Ok(IpcConfig::system(path, allowed_uids, allowed_gids, owner)?)
        }
    }
}

fn run_and_record_scan(
    store: &Store,
    state: &DaemonIpcState,
) -> Result<usize, Box<dyn std::error::Error>> {
    let count = run_daemon_iteration(store)?;
    state.record_scan(current_timestamp(), count);
    Ok(count)
}

/// Execute the `edgedisco daemon` command.
pub async fn run_daemon(args: &DaemonArgs) -> Result<(), Box<dyn std::error::Error>> {
    let mut base = args.clone();
    if base.config.is_none() && base.ipc_mode == IpcModeArg::User {
        let home = std::env::var_os("HOME").ok_or("HOME is required for user settings")?;
        base.config = Some(PathBuf::from(home).join(".edgedisco/config/daemon.json"));
    }
    let resolved = crate::config::resolve(&base)?;
    let args = &resolved;
    let policy = ipc_config(args)?;
    if args.check_ready {
        return crate::config::check_ready(&policy.socket_path).await;
    }
    let db_path = default_database_path(None, args.db.as_deref());
    let store = Arc::new(Store::open(&db_path)?);
    if args.prepare {
        println!("Configuration and database preparation succeeded");
        return Ok(());
    }

    println!(
        "EdgeDisco daemon initialized (database: {}, interval: {}s)",
        db_path.display(),
        args.interval
    );

    if args.once {
        let count = run_daemon_iteration(&store)?;
        export_outbox_if_configured(&store, args).await?;
        println!("Single collection iteration complete: {count} assets discovered and updated.");
        return Ok(());
    }

    let state = Arc::new(DaemonIpcState::new(current_timestamp()));
    let (scan_tx, mut scan_rx) = mpsc::channel::<ScanCommand>(8);
    let (settings_tx, mut settings_rx) = watch::channel(resolved.clone());
    let writable_path = if args.ipc_mode == IpcModeArg::User {
        base.config.clone()
    } else {
        None
    };
    let settings = Arc::new(SettingsManager::new(
        writable_path,
        base,
        resolved.clone(),
        settings_tx,
    ));
    let server = IpcServer::bind(policy, Arc::clone(&store), Arc::clone(&state), scan_tx)
        .await?
        .with_settings(settings);
    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    let ipc_task = tokio::spawn(server.serve(shutdown_rx));
    let export_task = spawn_export_worker(
        Arc::clone(&store),
        settings_rx.clone(),
        shutdown_tx.subscribe(),
    );

    let mut active = resolved.clone();
    let mut interval = tokio::time::interval(Duration::from_secs(active.interval));
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

    loop {
        tokio::select! {
            _ = interval.tick() => {
                match run_and_record_scan(&store, &state) {
                    Ok(count) => {
                        eprintln!("[{}] Observation pass complete: {} assets", current_timestamp(), count);
                    }
                    Err(e) => {
                        eprintln!("[{}] Error during observation pass: {}", current_timestamp(), e);
                    }
                }
            }
            Some(command) = scan_rx.recv() => {
                let result = run_and_record_scan(&store, &state).map_err(|error| error.to_string());
                let _ = command.reply.send(result);
            }
            changed = settings_rx.changed() => {
                if changed.is_ok() {
                    active = settings_rx.borrow_and_update().clone();
                    interval = tokio::time::interval(Duration::from_secs(active.interval));
                    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
                }
            }
            _ = signal::ctrl_c() => {
                println!("\nReceived shutdown signal; shutting down EdgeDisco daemon.");
                break;
            }
        }
    }

    let _ = shutdown_tx.send(true);
    ipc_task.await??;
    export_task.await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Cli, Commands};
    use clap::Parser;
    use edgedisco_core::exporter::ExporterConfig;
    use edgedisco_core::models::OutboxRecord;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    #[tokio::test]
    async fn export_worker_delivers_queued_record_without_a_scan() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("collector listener");
        let address = listener.local_addr().expect("collector address");
        let collector = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.expect("collector connection");
            let mut received = Vec::new();
            loop {
                let mut chunk = [0_u8; 4096];
                let size = stream.read(&mut chunk).await.expect("request bytes");
                assert!(size > 0, "request ended before body");
                received.extend_from_slice(&chunk[..size]);
                if let Some(header_end) = received.windows(4).position(|w| w == b"\r\n\r\n") {
                    let headers = String::from_utf8_lossy(&received[..header_end]);
                    let length = headers
                        .lines()
                        .find_map(|line| {
                            line.split_once(':').and_then(|(name, value)| {
                                name.eq_ignore_ascii_case("content-length")
                                    .then(|| value.trim().parse::<usize>().unwrap())
                            })
                        })
                        .expect("content length");
                    if received.len() >= header_end + 4 + length {
                        break;
                    }
                }
            }
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Type: application/x-protobuf\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                .await
                .expect("collector response");
        });

        let store = Arc::new(Store::open_in_memory().expect("store"));
        let payload = include_str!("../../../../tests/fixtures/golden_otlp/observation_v2.json");
        let now = current_timestamp();
        store
            .insert_outbox(&OutboxRecord::new(
                "worker-record",
                "worker-asset",
                payload,
                payload.len() as i64,
                &now,
                &now,
            ))
            .expect("queue record");
        let exporter = OtlpExporter::new(ExporterConfig::for_endpoint(format!(
            "http://{address}/v1/logs"
        )))
        .expect("exporter");
        let cli = Cli::parse_from(["edgedisco", "daemon"]);
        let Commands::Daemon(mut args) = cli.command else {
            panic!()
        };
        let (settings_tx, settings_rx) = watch::channel(args.clone());
        let (shutdown, receiver) = watch::channel(false);
        let worker = spawn_export_worker(Arc::clone(&store), settings_rx, receiver);
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert_eq!(
            store.get_outbox("worker-record").unwrap().unwrap().status,
            "pending"
        );
        args.otlp_exporter = Some(Box::new(exporter));
        settings_tx.send_replace(args);
        tokio::time::timeout(Duration::from_secs(3), collector)
            .await
            .expect("collector received request")
            .expect("collector task");
        tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                if store.get_outbox("worker-record").unwrap().unwrap().status == "delivered" {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        })
        .await
        .expect("worker marked event delivered");
        shutdown.send(true).expect("shutdown worker");
        worker.await.expect("worker task");
    }
}
