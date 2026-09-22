use edgedisco_cli::commands::scan::{
    combine_discovery_assets, generate_scan_report, persist_scan_report,
};
use edgedisco_core::encode_otlp_request;
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
fn installed_cli_remains_present_after_its_process_stops() {
    let temp = tempfile::tempdir().unwrap();
    let bin = temp.path().canonicalize().unwrap().join("bin");
    std::fs::create_dir(&bin).unwrap();
    std::fs::write(bin.join("opencode"), b"fixture").unwrap();
    let installed = edgedisco_sensor::installed::scan_installed_clis_in(
        std::slice::from_ref(&bin),
        std::slice::from_ref(&bin),
    );
    assert_eq!(installed.len(), 1);
    let process = Asset::new(
        edgedisco_core::redaction::sha256_digest("running opencode"),
        "process",
        "OpenCode",
        "OpenCode",
        true,
    );
    let mut report = ScanReport {
        schema_version: Some(2),
        scan_id: "with-process".into(),
        observed_at: "2026-09-22T00:00:00Z".into(),
        device: DeviceReport {
            hostname: "fixture".into(),
            os: "Darwin".into(),
            os_version: None,
            machine: None,
            agent_version: None,
        },
        assets: combine_discovery_assets(vec![process], installed.clone()),
        privacy: PrivacyFlags::default(),
    };
    // Native reports omit storage-managed presence; ingestion establishes it.
    for asset in &mut report.assets {
        asset.present = None;
    }
    validate_report(&report).unwrap();
    let db = temp.path().join("inventory.db");
    persist_scan_report(&db, &report).unwrap();
    report.scan_id = "idle-installation".into();
    report.observed_at = "2026-09-22T00:01:00Z".into();
    report.assets = installed;
    for asset in &mut report.assets {
        asset.present = None;
    }
    persist_scan_report(&db, &report).unwrap();
    let rows = Store::open(&db)
        .unwrap()
        .list_assets(None, None, false, 10)
        .unwrap();
    let app = rows.iter().find(|a| a.kind == "application").unwrap();
    assert_eq!(app.present, Some(true));
    assert!(!app.running);
    let process = rows.iter().find(|a| a.kind == "process").unwrap();
    assert_eq!(process.present, Some(false));
    assert!(!process.running);
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

#[test]
fn test_persisted_scans_reconcile_disappeared_assets_and_queue_otlp() {
    let temp = tempfile::NamedTempFile::new().expect("temporary database");
    let asset = Asset::new(
        edgedisco_core::redaction::sha256_digest("process-test"),
        "process",
        "Ollama",
        "Ollama",
        true,
    );
    let mut report = ScanReport {
        schema_version: Some(2),
        scan_id: "scan-present".into(),
        observed_at: "2026-09-22T00:00:00Z".into(),
        device: DeviceReport {
            hostname: "test-host".into(),
            os: "test-os".into(),
            os_version: None,
            machine: Some("test-machine".into()),
            agent_version: Some("0.1.0".into()),
        },
        assets: vec![asset],
        privacy: PrivacyFlags::default(),
    };

    persist_scan_report(temp.path(), &report).expect("persist present scan");
    let store = Store::open(temp.path()).expect("reopen store");
    let initial = store
        .list_outbox(Some("pending"), 10)
        .expect("list initial outbox");
    assert_eq!(initial.len(), 2, "asset transition and device heartbeat");
    let rows = initial.iter().collect::<Vec<_>>();
    encode_otlp_request(&rows).expect("native outbox rows must satisfy the OTLP contract");
    assert!(initial.iter().all(|record| {
        let payload: serde_json::Value =
            serde_json::from_str(&record.payload_json).expect("valid event payload");
        payload
            .get("event.name")
            .and_then(|value| value.as_str())
            .is_some()
            && payload
                .get("attributes")
                .and_then(|value| value.as_object())
                .is_some()
    }));

    report.scan_id = "scan-absent".into();
    report.observed_at = "2026-09-22T00:01:00Z".into();
    report.assets.clear();
    persist_scan_report(temp.path(), &report).expect("persist empty scan");

    let assets = store
        .list_assets(None, Some("process"), false, 10)
        .expect("list reconciled assets");
    assert_eq!(assets.len(), 1);
    assert!(!assets[0].running);
    assert_eq!(assets[0].present, Some(false));

    let queued = store
        .list_outbox(Some("pending"), 10)
        .expect("list replacement outbox");
    assert_eq!(queued.len(), 2);
    let disappearance = queued
        .iter()
        .filter_map(|record| serde_json::from_str::<serde_json::Value>(&record.payload_json).ok())
        .find(|payload| payload["event.name"] == "edgedisco.asset.observed")
        .expect("queued disappearance event");
    assert_eq!(disappearance["attributes"]["asset.present"], false);
    assert_eq!(disappearance["attributes"]["asset.running"], false);
}

#[test]
fn test_local_device_identity_is_stable_but_enrollment_secret_is_random() {
    let first = tempfile::NamedTempFile::new().expect("first database");
    let second = tempfile::NamedTempFile::new().expect("second database");
    let report = ScanReport {
        schema_version: Some(2),
        scan_id: "scan-identity".into(),
        observed_at: "2026-09-22T00:00:00Z".into(),
        device: DeviceReport {
            hostname: "same-host".into(),
            os: "same-os".into(),
            os_version: None,
            machine: Some("same-machine".into()),
            agent_version: None,
        },
        assets: Vec::new(),
        privacy: PrivacyFlags::default(),
    };

    persist_scan_report(first.path(), &report).expect("persist first identity");
    persist_scan_report(second.path(), &report).expect("persist second identity");
    let first_device = Store::open(first.path())
        .expect("open first")
        .list_devices(1)
        .expect("list first")
        .remove(0);
    let second_device = Store::open(second.path())
        .expect("open second")
        .list_devices(1)
        .expect("list second")
        .remove(0);

    assert_eq!(first_device.id, second_device.id);
    assert_eq!(first_device.id.len(), 32);
    assert!(first_device.id.bytes().all(|byte| byte.is_ascii_hexdigit()));
    assert_ne!(first_device.token_hash, second_device.token_hash);
    assert_eq!(first_device.token_hash.len(), 64);
}
