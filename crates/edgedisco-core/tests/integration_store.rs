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
fn test_version_three_database_adds_presence_column_before_advancing_version() {
    let dir = tempdir().expect("temp dir");
    let db_path = dir.path().join("v3.db");
    drop(Store::open(&db_path).expect("create current store"));
    {
        let conn = Connection::open(&db_path).expect("open raw conn");
        conn.execute_batch("ALTER TABLE assets DROP COLUMN present; PRAGMA user_version = 3;")
            .expect("simulate v3 store");
    }

    let store = Store::open(&db_path).expect("migrate v3 store");
    assert!(store
        .list_assets(None, None, false, 10)
        .expect("query migrated assets")
        .is_empty());
    let conn = Connection::open(&db_path).expect("inspect migrated database");
    let columns: Vec<String> = conn
        .prepare("PRAGMA table_info(assets)")
        .expect("prepare table info")
        .query_map([], |row| row.get(1))
        .expect("query columns")
        .collect::<Result<_, _>>()
        .expect("collect columns");
    assert!(columns.contains(&"present".to_string()));
    let version: u32 = conn
        .query_row("PRAGMA user_version", [], |row| row.get(0))
        .expect("read version");
    assert_eq!(version, DATABASE_VERSION);
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
fn migration_backup_preserves_committed_wal_and_original_version() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("inventory.db");
    drop(Store::open(&path).unwrap());
    let conn = Connection::open(&path).unwrap();
    conn.execute_batch(
        "PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0;
        CREATE TABLE recovery_marker(value TEXT);
        INSERT INTO recovery_marker VALUES('preserve me');
        ALTER TABLE assets DROP COLUMN present; PRAGMA user_version=3;",
    )
    .unwrap();
    drop(Store::open(&path).unwrap());
    let backups: Vec<_> = std::fs::read_dir(dir.path())
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|p| p.is_dir())
        .collect();
    assert_eq!(backups.len(), 1);
    let backup = Connection::open(backups[0].join("inventory.db")).unwrap();
    let version: u32 = backup
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap();
    let marker: String = backup
        .query_row("SELECT value FROM recovery_marker", [], |r| r.get(0))
        .unwrap();
    assert_eq!(version, 3);
    assert_eq!(marker, "preserve me");
    drop(Store::open(&path).unwrap());
    assert_eq!(
        std::fs::read_dir(dir.path())
            .unwrap()
            .filter(|e| e.as_ref().unwrap().path().is_dir())
            .count(),
        1
    );
}

#[test]
fn failed_migration_keeps_version_and_rolls_back_schema_changes() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("inventory.db");
    let conn = Connection::open(&path).unwrap();
    // Deliberately incompatible historical schema fails during index creation.
    conn.execute_batch("CREATE TABLE assets(broken TEXT); PRAGMA user_version=3;")
        .unwrap();
    assert!(Store::open(&path).is_err());
    let version: u32 = conn
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap();
    let devices: u32 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='devices'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(version, 3);
    assert_eq!(devices, 0);
    assert!(std::fs::read_dir(dir.path())
        .unwrap()
        .any(|e| e.unwrap().path().is_dir()));
}

#[test]
fn legacy_outbox_migration_preserves_pending_payload_and_view() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("legacy.db");
    let conn = Connection::open(&path).unwrap();
    conn.execute_batch("CREATE TABLE otlp_outbox (
        id TEXT PRIMARY KEY, asset_key TEXT NOT NULL, payload_json TEXT NOT NULL,
        payload_bytes INTEGER NOT NULL, status TEXT NOT NULL, attempt_count INTEGER NOT NULL,
        next_attempt_at TEXT NOT NULL, created_at TEXT NOT NULL, delivered_at TEXT, last_error_code TEXT);
        INSERT INTO otlp_outbox VALUES('id','asset','{}',2,'pending',0,'now','now',NULL,NULL);
        CREATE VIEW outbox AS SELECT * FROM otlp_outbox;
        PRAGMA user_version=2;").unwrap();
    drop(Store::open(&path).unwrap());
    let payload: String = conn
        .query_row("SELECT payload_json FROM outbox WHERE id='id'", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert_eq!(payload, "{}");
    let lease: Option<String> = conn
        .query_row("SELECT lease_id FROM outbox WHERE id='id'", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert!(lease.is_none());
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

#[test]
fn test_expired_outbox_lease_is_recovered() {
    let store = Store::open_in_memory().expect("open store");
    let record = OutboxRecord::new(
        "expired-1",
        "asset-expired",
        "{}",
        2,
        "2026-09-22T00:00:00Z",
        "2026-09-22T00:00:00Z",
    );
    store.insert_outbox(&record).expect("insert record");
    let first = store
        .claim_outbox(
            10,
            1024,
            "lease-old",
            "2026-09-22T00:01:00Z",
            "2026-09-22T00:00:00Z",
        )
        .expect("first claim");
    assert_eq!(first.len(), 1);

    let recovered = store
        .claim_outbox(
            10,
            1024,
            "lease-new",
            "2026-09-22T00:03:00Z",
            "2026-09-22T00:02:00Z",
        )
        .expect("recover expired claim");
    assert_eq!(recovered.len(), 1);
    assert_eq!(recovered[0].id, "expired-1");
    assert_eq!(recovered[0].lease_id.as_deref(), Some("lease-new"));
    assert_eq!(recovered[0].attempt_count, 0);
    assert_eq!(
        recovered[0].last_error_code.as_deref(),
        Some("lease_expired")
    );
}
