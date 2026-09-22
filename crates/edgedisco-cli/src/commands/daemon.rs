use crate::cli::DaemonArgs;
use crate::util::{
    current_timestamp, default_database_path, get_hostname, get_machine, get_os_name,
    get_os_version,
};
use edgedisco_core::exporter::{ExporterConfig, OtlpExporter};
use edgedisco_core::models::Device;
use edgedisco_core::redaction::sha256_digest;
use edgedisco_core::store::Store;
use edgedisco_sensor::{scan_available_containers, scan_host_processes, scan_processes};
use std::time::Duration;
use tokio::signal;

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

/// Execute the `edgedisco daemon` command.
pub async fn run_daemon(args: &DaemonArgs) -> Result<(), Box<dyn std::error::Error>> {
    let db_path = default_database_path(None, args.db.as_deref());
    let store = Store::open(&db_path)?;

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

    let interval_duration = Duration::from_secs(args.interval);
    let mut interval = tokio::time::interval(interval_duration);

    loop {
        tokio::select! {
            _ = interval.tick() => {
                match run_daemon_iteration(&store) {
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
            _ = signal::ctrl_c() => {
                println!("\nReceived shutdown signal; shutting down EdgeDisco daemon.");
                break;
            }
        }
    }

    Ok(())
}
