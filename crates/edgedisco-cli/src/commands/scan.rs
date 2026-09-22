use crate::cli::ScanArgs;
use crate::util::{current_timestamp, get_hostname, get_machine, get_os_name, get_os_version};
use edgedisco_core::models::{DeviceReport, PrivacyFlags, ScanReport};
use edgedisco_sensor::{scan_host_processes, scan_processes};
use uuid::Uuid;

/// Run native process observation, match against AI detection catalog, and build a ScanReport.
pub fn generate_scan_report() -> Result<ScanReport, Box<dyn std::error::Error>> {
    let observations = scan_host_processes()?;
    let assets = scan_processes(&observations);

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

/// Execute the `edgedisco scan` command.
pub fn run_scan(args: &ScanArgs) -> Result<(), Box<dyn std::error::Error>> {
    let report = generate_scan_report()?;
    if args.pretty {
        println!("{}", serde_json::to_string_pretty(&report)?);
    } else {
        println!("{}", serde_json::to_string(&report)?);
    }
    Ok(())
}
