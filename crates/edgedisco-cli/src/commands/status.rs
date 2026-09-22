use crate::cli::StatusArgs;
use crate::util::default_database_path;
use edgedisco_core::store::Store;
use std::path::Path;

/// Format database inventory status into human-readable text or structured JSON.
pub fn format_status(
    store: &Store,
    db_path: &Path,
    json_mode: bool,
) -> Result<String, Box<dyn std::error::Error>> {
    let devices = store.list_devices(5)?;
    let assets = store.list_assets(None, None, false, 10000)?;
    let running_assets = assets.iter().filter(|a| a.running).count();
    let sessions = store.list_sessions(None, Some("active"), 1000)?;

    let primary_device = devices.first();
    let enrolled = primary_device.is_some();

    if json_mode {
        let json_val = serde_json::json!({
            "version": env!("CARGO_PKG_VERSION"),
            "database": db_path.display().to_string(),
            "enrolled": enrolled,
            "device": primary_device.map(|d| serde_json::json!({
                "id": d.id,
                "hostname": d.hostname,
                "os": d.os,
                "os_version": d.os_version,
                "machine": d.machine,
                "enrolled_at": d.enrolled_at,
                "last_seen": d.last_seen,
            })),
            "assets_total": assets.len(),
            "assets_running": running_assets,
            "active_sessions": sessions.len(),
        });
        return Ok(serde_json::to_string_pretty(&json_val)?);
    }

    let mut out = Vec::new();
    out.push(format!("Version: {}", env!("CARGO_PKG_VERSION")));
    out.push(format!("Database: {}", db_path.display()));
    out.push(format!(
        "Endpoint: {}",
        if enrolled { "enrolled" } else { "not enrolled" }
    ));

    if let Some(dev) = primary_device {
        let os_str = dev.os_version.as_deref().unwrap_or(&dev.os);
        let machine_str = dev.machine.as_deref().unwrap_or("unknown");
        out.push(format!(
            "Hostname: {} ({} {})",
            dev.hostname, os_str, machine_str
        ));
        out.push(format!("Device ID: {}", dev.id));
        out.push(format!("Last Seen: {}", dev.last_seen));
    }

    out.push(format!(
        "Assets: {} ({} running)",
        assets.len(),
        running_assets
    ));
    out.push(format!("Active Sessions: {}", sessions.len()));

    Ok(out.join("\n"))
}

/// Execute the `edgedisco status` command.
pub fn run_status(args: &StatusArgs) -> Result<(), Box<dyn std::error::Error>> {
    let db_path = default_database_path(args.root.as_deref(), args.db.as_deref());

    if !db_path.exists() {
        if args.json {
            let json_val = serde_json::json!({
                "version": env!("CARGO_PKG_VERSION"),
                "database": db_path.display().to_string(),
                "enrolled": false,
                "status": "database_not_found",
                "assets_total": 0,
                "assets_running": 0,
                "active_sessions": 0,
            });
            println!("{}", serde_json::to_string_pretty(&json_val)?);
        } else {
            println!("Version: {}", env!("CARGO_PKG_VERSION"));
            println!("Database: {}", db_path.display());
            println!(
                "Status: database not initialized; run 'edgedisco scan' or 'edgedisco daemon'"
            );
        }
        return Ok(());
    }

    let store = Store::open(&db_path)?;
    let rendered = format_status(&store, &db_path, args.json)?;
    println!("{rendered}");
    Ok(())
}
