use edgedisco_cli::commands::scan::{
    combine_discovery_assets, generate_scan_report, persist_scan_report,
};
use edgedisco_core::models::{Asset, DeviceReport, PrivacyFlags, ScanReport};
use edgedisco_core::redaction::validate_report;
use edgedisco_core::store::Store;

#[test]
fn test_generate_scan_report_schema_compliance() {
    let report = generate_scan_report().expect("generate scan report");

    assert!(!report.scan_id.is_empty());
    assert!(!report.observed_at.is_empty());
    assert!(!report.device.hostname.is_empty());
    assert!(!report.device.os.is_empty());

    // Validate report against core domain rules
    validate_report(&report).expect("validate scan report schema");

    // Serialize to JSON and deserialize back
    let json_bytes = serde_json::to_vec(&report).expect("serialize report");
    let deserialized: ScanReport =
        serde_json::from_slice(&json_bytes).expect("deserialize scan report");

    assert_eq!(deserialized.scan_id, report.scan_id);
    assert_eq!(deserialized.assets.len(), report.assets.len());
}

#[test]
fn test_scan_combines_host_and_container_assets_without_duplicates() {
    let host = Asset::new("host-ollama", "application", "Ollama", "Ollama", true);
    let container = Asset::new(
        "container-ollama",
        "container_application",
        "Ollama",
        "Ollama",
        true,
    );
    let duplicate = container.clone();

    let assets = combine_discovery_assets(vec![host], vec![container, duplicate]);

    assert_eq!(assets.len(), 2);
    assert!(assets.iter().any(|asset| asset.kind == "application"));
    assert!(assets
        .iter()
        .any(|asset| asset.kind == "container_application"));
}

#[test]
fn test_scan_report_persists_container_assets() {
    let temp = tempfile::NamedTempFile::new().expect("temporary database");
    let asset = Asset::new(
        edgedisco_core::redaction::sha256_digest("container-test"),
        "container_application",
        "Ollama",
        "Ollama",
        true,
    );
    let report = ScanReport {
        schema_version: Some(1),
        scan_id: "scan-test".into(),
        observed_at: "2026-09-22T00:00:00Z".into(),
        device: DeviceReport {
            hostname: "test-host".into(),
            os: "test-os".into(),
            os_version: None,
            machine: None,
            agent_version: Some("0.1.0".into()),
        },
        assets: vec![asset.clone()],
        privacy: PrivacyFlags::default(),
    };

    persist_scan_report(temp.path(), &report).expect("persist scan");
    let store = Store::open(temp.path()).expect("reopen store");
    let assets = store
        .list_assets(None, Some("container_application"), true, 10)
        .expect("list persisted assets");
    assert_eq!(assets.len(), 1);
    assert_eq!(assets[0].fingerprint, asset.fingerprint);
    assert_eq!(assets[0].name, "Ollama");
    assert_eq!(
        assets[0].first_seen.as_deref(),
        Some(report.observed_at.as_str())
    );
    assert_eq!(
        assets[0].last_seen.as_deref(),
        Some(report.observed_at.as_str())
    );
}
