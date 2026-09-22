use edgedisco_core::models::{Asset, Device, OutboxRecord, Session};
use edgedisco_core::store::{Store, StoreError, DATABASE_VERSION};
use rusqlite::Connection;
use tempfile::tempdir;

#[test]
fn test_disk_store_creation_and_wal_mode() {
    let dir = tempdir().expect("temp dir");
    let db_path = dir.path().join("inventory.db");

    let store = Store::open(&db_path).expect("open disk store");
    let tables = store.table_names().expect("list tables");

    assert!(tables.contains(&"devices".to_string()));
    assert!(tables.contains(&"scans".to_string()));
    assert!(tables.contains(&"assets".to_string()));
    assert!(tables.contains(&"agent_sessions".to_string()));
    assert!(tables.contains(&"sessions".to_string()));
    assert!(tables.contains(&"otlp_outbox".to_string()));
    assert!(tables.contains(&"outbox".to_string()));
    assert!(tables.contains(&"runtime_events".to_string()));
    assert!(tables.contains(&"otlp_asset_state".to_string()));
    assert!(tables.contains(&"otlp_export_status".to_string()));
    assert!(tables.contains(&"inventory_sync_changes".to_string()));

    // Verify WAL mode via raw connection inspection
    let conn = Connection::open(&db_path).expect("open raw conn");
    let mode: String = conn
        .query_row("PRAGMA journal_mode", [], |row| row.get(0))
        .expect("journal mode");
    assert_eq!(mode.to_lowercase(), "wal");

    let version: u32 = conn
        .query_row("PRAGMA user_version", [], |row| row.get(0))
        .expect("user version");
    assert_eq!(version, DATABASE_VERSION);
}

#[test]
fn test_downgrade_prevention() {
    let dir = tempdir().expect("temp dir");
    let db_path = dir.path().join("future.db");

    {
        let conn = Connection::open(&db_path).expect("open raw conn");
        conn.execute("PRAGMA user_version = 999", [])
            .expect("set future version");
    }

    let result = Store::open(&db_path);
    match result {
        Err(StoreError::DowngradeRefused) => {}
        other => panic!("expected DowngradeRefused, got {other:?}"),
    }
}

#[test]
fn test_foreign_key_enforcement() {
    let store = Store::open_in_memory().expect("open store");

    let asset = Asset::new(
        "a94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
        "application",
        "Claude",
        "Anthropic",
        true,
    );

    // Attempting to insert an asset for a non-existent device must fail due to foreign key
    let res = store.upsert_asset("non_existent_device", &asset, "2026-09-21T00:00:00Z");
    assert!(
        res.is_err(),
        "Foreign key check should reject non-existent device_id"
    );
}

#[test]
fn test_full_device_and_asset_lifecycle() {
    let dir = tempdir().expect("temp dir");
    let db_path = dir.path().join("lifecycle.db");
    let store = Store::open(&db_path).expect("open store");

    let dev1 = Device::new(
        "dev-alpha",
        "mac-pro-1",
        "Darwin",
        Some("15.0".into()),
        Some("arm64".into()),
        Some("1.0.0".into()),
        "token_hash_alpha",
        "2026-09-21T10:00:00Z",
        "2026-09-21T10:00:00Z",
    );
    let dev2 = Device::new(
        "dev-beta",
        "linux-box-1",
        "Linux",
        Some("6.8.0".into()),
        Some("x86_64".into()),
        Some("1.0.0".into()),
        "token_hash_beta",
        "2026-09-21T10:05:00Z",
        "2026-09-21T10:05:00Z",
    );

    store.enroll_device(&dev1).expect("enroll dev1");
    store.enroll_device(&dev2).expect("enroll dev2");

    let devices = store.list_devices(10).expect("list devices");
    assert_eq!(devices.len(), 2);

    let mut asset1 = Asset::new(
        "1111111111111111111111111111111111111111111111111111111111111111",
        "application",
        "Cursor",
        "Anysphere",
        true,
    );
    asset1.version = Some("0.42.0".into());
    asset1
        .metadata
        .insert("executable".into(), serde_json::json!("cursor"));

    let mut asset2 = Asset::new(
        "2222222222222222222222222222222222222222222222222222222222222222",
        "process",
        "Ollama",
        "Ollama",
        false,
    );
    asset2
        .metadata
        .insert("executable".into(), serde_json::json!("ollama"));

    store
        .upsert_asset("dev-alpha", &asset1, "2026-09-21T10:00:00Z")
        .expect("upsert asset 1");
    store
        .upsert_asset("dev-alpha", &asset2, "2026-09-21T10:00:00Z")
        .expect("upsert asset 2");

    let all_assets = store
        .list_assets(Some("dev-alpha"), None, false, 10)
        .expect("list assets");
    assert_eq!(all_assets.len(), 2);

    let running_assets = store
        .list_assets(Some("dev-alpha"), None, true, 10)
        .expect("running only");
    assert_eq!(running_assets.len(), 1);
    assert_eq!(running_assets[0].name, "Cursor");

    let apps = store
        .list_assets(Some("dev-alpha"), Some("application"), false, 10)
        .expect("apps only");
    assert_eq!(apps.len(), 1);
    assert_eq!(apps[0].name, "Cursor");

    // Upsert update: change running state of asset 2 to true
    asset2.running = true;
    store
        .upsert_asset("dev-alpha", &asset2, "2026-09-21T10:10:00Z")
        .expect("update asset 2");

    let running_now = store
        .list_assets(Some("dev-alpha"), None, true, 10)
        .expect("running now");
    assert_eq!(running_now.len(), 2);
}

#[test]
fn test_session_lifecycle() {
    let store = Store::open_in_memory().expect("open store");
    let dev = Device::new(
        "dev-1",
        "macbook",
        "Darwin",
        None,
        None,
        None,
        "token_hash_1",
        "2026-09-21T00:00:00Z",
        "2026-09-21T00:00:00Z",
    );
    store.enroll_device(&dev).expect("enroll");

    let session = Session::new(
        "dev-1",
        "sess-100",
        "agent-200",
        "Claude Code",
        "active",
        "2026-09-21T00:00:00Z",
        "2026-09-21T00:00:00Z",
        "message_start",
    );
    store.upsert_session(&session).expect("upsert session");

    let retrieved = store
        .get_session("dev-1", "sess-100", "agent-200")
        .expect("get")
        .expect("found");
    assert_eq!(retrieved.event_count, 1);
    assert_eq!(retrieved.status, "active");

    // Re-upserting increments event_count
    store.upsert_session(&session).expect("re-upsert");
    let updated = store
        .get_session("dev-1", "sess-100", "agent-200")
        .expect("get")
        .expect("found");
    assert_eq!(updated.event_count, 2);
}

#[test]
fn test_outbox_claiming_and_leasing() {
    let store = Store::open_in_memory().expect("open store");

    for i in 1..=5 {
        let rec = OutboxRecord::new(
            format!("out-{i}"),
            format!("asset-key-{i}"),
            format!(r#"{{"index":{i}}}"#),
            20,
            "2026-09-21T00:00:00Z",
            "2026-09-21T00:00:00Z",
        );
        store.insert_outbox(&rec).expect("insert");
    }

    // Claim batch of 3 records
    let claimed = store
        .claim_outbox(
            3,
            1000,
            "lease-a",
            "2026-09-21T00:05:00Z",
            "2026-09-21T00:00:00Z",
        )
        .expect("claim");
    assert_eq!(claimed.len(), 3);
    assert_eq!(claimed[0].id, "out-1");
    assert_eq!(claimed[1].id, "out-2");
    assert_eq!(claimed[2].id, "out-3");

    // Next claim should only get remaining 2 records
    let claimed2 = store
        .claim_outbox(
            3,
            1000,
            "lease-b",
            "2026-09-21T00:05:00Z",
            "2026-09-21T00:00:00Z",
        )
        .expect("claim 2");
    assert_eq!(claimed2.len(), 2);
    assert_eq!(claimed2[0].id, "out-4");
    assert_eq!(claimed2[1].id, "out-5");

    // Finish first batch as delivered
    let finished = store
        .finish_outbox(
            &["out-1", "out-2", "out-3"],
            "delivered",
            None,
            Some(200),
            "2026-09-21T00:01:00Z",
        )
        .expect("finish");
    assert_eq!(finished, 3);

    let delivered = store
        .list_outbox(Some("delivered"), 10)
        .expect("list delivered");
    assert_eq!(delivered.len(), 3);

    // Finish second batch as retry
    let retried = store
        .finish_outbox(
            &["out-4", "out-5"],
            "retry",
            Some("transport"),
            Some(503),
            "2026-09-21T00:01:00Z",
        )
        .expect("retry");
    assert_eq!(retried, 2);

    let retry_list = store.list_outbox(Some("retry"), 10).expect("list retry");
    assert_eq!(retry_list.len(), 2);
}
