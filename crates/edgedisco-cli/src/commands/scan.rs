use crate::cli::ScanArgs;
use crate::util::{
    current_timestamp, default_database_path, get_hostname, get_machine, get_os_name,
    get_os_version, local_device_id, random_token_hash,
};
use edgedisco_core::models::{Asset, Device, DeviceReport, PrivacyFlags, ScanReport};
use edgedisco_core::store::Store;
use edgedisco_sensor::{
    scan_available_containers, scan_editor_extensions, scan_host_processes, scan_installed_apps,
    scan_installed_clis, scan_mcp_configs, scan_processes,
};
use std::collections::HashSet;
use std::path::Path;
use uuid::Uuid;

/// Merge host and container discovery while suppressing repeated fingerprints.
pub fn combine_discovery_assets(host: Vec<Asset>, containers: Vec<Asset>) -> Vec<Asset> {
    let mut seen = HashSet::new();
    host.into_iter()
        .chain(containers)
        .filter(|asset| seen.insert(asset.fingerprint.clone()))
        .collect()
}

/// Merge installed CLI inventory, native processes, and local containers into a report.
pub fn generate_scan_report() -> Result<ScanReport, Box<dyn std::error::Error>> {
    let observations = scan_host_processes()?;
    let host = combine_discovery_assets(scan_processes(&observations), scan_installed_clis());
    let host = combine_discovery_assets(host, scan_installed_apps());
    let host = combine_discovery_assets(host, scan_editor_extensions());
    let host = combine_discovery_assets(host, scan_mcp_configs());
    let mut assets = combine_discovery_assets(host, scan_available_containers());
    for asset in &mut assets {
        asset.present = None;
    }

    let device = DeviceReport {
        hostname: get_hostname(),
        os: get_os_name(),
        os_version: get_os_version(),
        machine: get_machine(),
        agent_version: Some(env!("CARGO_PKG_VERSION").to_string()),
    };

    let report = ScanReport {
        schema_version: Some(2),
        scan_id: format!("scan-{}", Uuid::new_v4()),
        observed_at: current_timestamp(),
        device,
        assets,
        privacy: PrivacyFlags::default(),
    };

    Ok(report)
}

/// Persist a completed scan and all host/container assets into the local SQLite inventory.
pub fn persist_scan_report(
    database_path: &Path,
    report: &ScanReport,
) -> Result<(), Box<dyn std::error::Error>> {
    let store = Store::open(database_path)?;
    persist_scan_report_to_store(&store, report)?;
    Ok(())
}

pub fn persist_scan_report_to_store(
    store: &Store,
    report: &ScanReport,
) -> Result<usize, Box<dyn std::error::Error>> {
    let device_id = local_device_id(
        &report.device.hostname,
        &report.device.os,
        report.device.machine.as_deref(),
    );
    let received_at = current_timestamp();
    let device = Device::new(
        device_id.clone(),
        report.device.hostname.clone(),
        report.device.os.clone(),
        report.device.os_version.clone(),
        report.device.machine.clone(),
        report.device.agent_version.clone(),
        random_token_hash(),
        report.observed_at.clone(),
        received_at,
    );
    Ok(store.ingest_scan(&device, report, true)?)
}

/// Execute the `edgedisco scan` command.
pub fn run_scan(args: &ScanArgs) -> Result<(), Box<dyn std::error::Error>> {
    let report = generate_scan_report()?;
    let database_path = args
        .db
        .clone()
        .or_else(|| args.persist.then(|| default_database_path(None, None)));
    if let Some(database_path) = database_path {
        persist_scan_report(&database_path, &report)?;
    }
    if args.pretty {
        println!("{}", serde_json::to_string_pretty(&report)?);
    } else {
        println!("{}", serde_json::to_string(&report)?);
    }
    Ok(())
}
