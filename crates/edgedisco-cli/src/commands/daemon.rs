use crate::cli::{DaemonArgs, IpcModeArg};
use crate::ipc::{default_user_socket_path, DaemonIpcState, IpcConfig, IpcServer, ScanCommand};
use crate::util::{
    current_timestamp, default_database_path, get_hostname, get_machine, get_os_name,
    get_os_version,
};
use edgedisco_core::exporter::{ExporterConfig, OtlpExporter};
use edgedisco_core::models::Device;
use edgedisco_core::redaction::sha256_digest;
use edgedisco_core::store::Store;
use edgedisco_sensor::{scan_available_containers, scan_host_processes, scan_processes};
use std::collections::BTreeSet;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tokio::signal;
use tokio::sync::{mpsc, watch};

/// Ensure an enrolled local device record exists in the store, returning the device ID.
pub fn ensure_local_device(store: &Store) -> Result<String, Box<dyn std::error::Error>> {
    let devices = store.list_devices(1)?;
    if let Some(dev) = devices.first() {
        return Ok(dev.id.clone());
    }

    let hostname = get_hostname();
    let dev_id = format!("dev-{}", &sha256_digest(format!("local:{hostname}"))[0..16]);
    let now = current_timestamp();
    let token_hash = sha256_digest(format!("token:{dev_id}"));

    let device = Device::new(
        dev_id.clone(),
        hostname,
        get_os_name(),
        get_os_version(),
        get_machine(),
        Some(env!("CARGO_PKG_VERSION").to_string()),
        token_hash,
        now.clone(),
        now,
    );

    store.enroll_device(&device)?;
    Ok(dev_id)
}

/// Run a single collection iteration: process scan, catalog classification, and SQLite upsert.
pub fn run_daemon_iteration(store: &Store) -> Result<usize, Box<dyn std::error::Error>> {
    let device_id = ensure_local_device(store)?;
    let now = current_timestamp();

    let observations = scan_host_processes()?;
    let assets = super::scan::combine_discovery_assets(
        scan_processes(&observations),
        scan_available_containers(),
    );
    let asset_count = assets.len();

    for asset in &assets {
        store.upsert_asset(&device_id, asset, &now)?;
    }

    Ok(asset_count)
}

async fn export_outbox_if_configured(
    store: &Store,
    args: &DaemonArgs,
) -> Result<(), Box<dyn std::error::Error>> {
    let Some(endpoint) = &args.otlp_endpoint else {
        return Ok(());
    };
    let mut config = ExporterConfig::for_endpoint(endpoint);
    config.batch_records = args.otlp_batch_size;
    let exporter = OtlpExporter::new(config)?;
    let outcome = exporter.export_once_at(store, &current_timestamp()).await?;
    if outcome.claimed > 0 {
        eprintln!(
            "[{}] OTLP export: {} delivered, {} scheduled for retry",
            current_timestamp(),
            outcome.delivered,
            outcome.retried
        );
    }
    Ok(())
}

fn ipc_config(args: &DaemonArgs) -> Result<IpcConfig, Box<dyn std::error::Error>> {
    match args.ipc_mode {
        IpcModeArg::User => {
            if !args.ipc_allowed_uid.is_empty()
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
            let allowed = args
                .ipc_allowed_uid
                .iter()
                .copied()
                .collect::<BTreeSet<_>>();
            let owner = args.ipc_owner_uid.zip(args.ipc_group_gid);
            Ok(IpcConfig::system(path, allowed, owner)?)
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
    let db_path = default_database_path(None, args.db.as_deref());
    let store = Arc::new(Store::open(&db_path)?);

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
    let server = IpcServer::bind(
        ipc_config(args)?,
        Arc::clone(&store),
        Arc::clone(&state),
        scan_tx,
    )
    .await?;
    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    let ipc_task = tokio::spawn(server.serve(shutdown_rx));

    let interval_duration = Duration::from_secs(args.interval);
    let mut interval = tokio::time::interval(interval_duration);

    loop {
        tokio::select! {
            _ = interval.tick() => {
                match run_and_record_scan(&store, &state) {
                    Ok(count) => {
                        eprintln!("[{}] Observation pass complete: {} assets", current_timestamp(), count);
                        if let Err(e) = export_outbox_if_configured(&store, args).await {
                            eprintln!("[{}] OTLP export error: {}", current_timestamp(), e);
                        }
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
            _ = signal::ctrl_c() => {
                println!("\nReceived shutdown signal; shutting down EdgeDisco daemon.");
                break;
            }
        }
    }

    let _ = shutdown_tx.send(true);
    ipc_task.await??;
    Ok(())
}
