use crate::cli::ScanArgs;
use crate::util::{current_timestamp, get_hostname, get_machine, get_os_name, get_os_version};
use edgedisco_core::models::{Asset, Device, DeviceReport, PrivacyFlags, ScanReport};
use edgedisco_core::redaction::sha256_digest;
use edgedisco_core::store::Store;
use edgedisco_sensor::{scan_available_containers, scan_host_processes, scan_processes};
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

/// Run native process and local container observation, then build a ScanReport.
pub fn generate_scan_report() -> Result<ScanReport, Box<dyn std::error::Error>> {
    let observations = scan_host_processes()?;
    let assets =
        combine_discovery_assets(scan_processes(&observations), scan_available_containers());

    let device = DeviceReport {
        hostname: get_hostname(),
        os: get_os_name(),
        os_version: get_os_version(),
        machine: get_machine(),
        agent_version: Some(env!("CARGO_PKG_VERSION").to_string()),
    };

    let report = ScanReport {
        schema_version: Some(1),
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
    let device_id = format!(
        "dev-{}",
        &sha256_digest(format!(
            "{}:{}:{}",
            report.device.hostname,
            report.device.os,
            report.device.machine.as_deref().unwrap_or("")
        ))[..16]
    );
    let device = Device::new(
        device_id.clone(),
        report.device.hostname.clone(),
        report.device.os.clone(),
        report.device.os_version.clone(),
        report.device.machine.clone(),
        report.device.agent_version.clone(),
        sha256_digest(format!("token:{device_id}")),
        report.observed_at.clone(),
        report.observed_at.clone(),
    );
    store.enroll_device(&device)?;
    for asset in &report.assets {
        store.upsert_asset(&device_id, asset, &report.observed_at)?;
    }
    Ok(())
}

/// Execute the `edgedisco scan` command.
pub fn run_scan(args: &ScanArgs) -> Result<(), Box<dyn std::error::Error>> {
    let report = generate_scan_report()?;
    if let Some(database_path) = &args.db {
        persist_scan_report(database_path, &report)?;
    }
    if args.pretty {
        println!("{}", serde_json::to_string_pretty(&report)?);
    } else {
        println!("{}", serde_json::to_string(&report)?);
    }
    Ok(())
}
