use crate::models::{Asset, Device, OutboxRecord, Session};
use rusqlite::{params, Connection, OptionalExtension};
use std::path::Path;
use std::sync::Mutex;
use thiserror::Error;

pub const DATABASE_VERSION: u32 = 4;

#[derive(Debug, Error)]
pub enum StoreError {
    #[error("sqlite error: {0}")]
    Sqlite(#[from] rusqlite::Error),
    #[error("json serialization error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("database was created by a newer EdgeDisco release; refusing downgrade")]
    DowngradeRefused,
    #[error("invalid argument: {0}")]
    InvalidArgument(String),
}

/// Embedded SQLite database store managing devices, assets, sessions, and telemetry outbox.
pub struct Store {
    conn: Mutex<Connection>,
}

impl std::fmt::Debug for Store {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Store").finish()
    }
}

impl Store {
    /// Open or create SQLite store at a given filesystem path.
    pub fn open(path: impl AsRef<Path>) -> Result<Self, StoreError> {
        let path = path.as_ref();
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent).map_err(|e| {
                    StoreError::InvalidArgument(format!("cannot create directory: {e}"))
                })?;
            }
        }
        let conn = Connection::open(path)?;
        let store = Self {
            conn: Mutex::new(conn),
        };
        store.initialize()?;
        Ok(store)
    }

    /// Open an isolated in-memory SQLite store.
    pub fn open_in_memory() -> Result<Self, StoreError> {
        let conn = Connection::open_in_memory()?;
        let store = Self {
            conn: Mutex::new(conn),
        };
        store.initialize()?;
        Ok(store)
    }

    /// Initialize SQLite schema with WAL mode and foreign key constraints.
    pub fn initialize(&self) -> Result<(), StoreError> {
        let conn = self.conn.lock().unwrap();

        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")?;

        let version: u32 = conn.query_row("PRAGMA user_version", [], |row| row.get(0))?;
        if version > DATABASE_VERSION {
            return Err(StoreError::DowngradeRefused);
        }

        conn.execute_batch(
            r#"
            BEGIN IMMEDIATE;

            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                hostname TEXT NOT NULL,
                os TEXT NOT NULL,
                os_version TEXT,
                machine TEXT,
                agent_version TEXT,
                token_hash TEXT UNIQUE NOT NULL,
                enrolled_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scans (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL REFERENCES devices(id),
                observed_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                asset_count INTEGER NOT NULL,
                privacy_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS assets (
                device_id TEXT NOT NULL REFERENCES devices(id),
                fingerprint TEXT NOT NULL,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                vendor TEXT NOT NULL,
                version TEXT,
                path_hash TEXT,
                command_hash TEXT,
                binary_sha256 TEXT,
                binary_fingerprint_status TEXT,
                fingerprint_library_version TEXT,
                metadata_json TEXT NOT NULL,
                running INTEGER NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                present INTEGER CHECK(present IN (0,1)),
                PRIMARY KEY(device_id, fingerprint)
            );

            CREATE INDEX IF NOT EXISTS idx_assets_name ON assets(name);
            CREATE INDEX IF NOT EXISTS idx_assets_seen ON assets(last_seen);
            CREATE INDEX IF NOT EXISTS idx_scans_device_received ON scans(device_id, received_at);
            CREATE INDEX IF NOT EXISTS idx_scans_device_observed ON scans(device_id, observed_at);

            CREATE TABLE IF NOT EXISTS agent_sessions (
                device_id TEXT NOT NULL REFERENCES devices(id),
                session_hash TEXT NOT NULL,
                agent_hash TEXT NOT NULL,
                app TEXT NOT NULL,
                agent_type TEXT,
                model TEXT,
                status TEXT NOT NULL,
                workspace_hash TEXT,
                user_hash TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                last_event TEXT NOT NULL,
                event_count INTEGER NOT NULL,
                tool_count INTEGER NOT NULL,
                mcp_count INTEGER NOT NULL,
                duration_ms INTEGER,
                PRIMARY KEY(device_id, session_hash, agent_hash)
            );

            CREATE INDEX IF NOT EXISTS idx_agent_sessions_seen ON agent_sessions(last_seen);

            CREATE TABLE IF NOT EXISTS otlp_outbox (
                id TEXT PRIMARY KEY,
                asset_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_bytes INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','retry','sending','delivered','failed')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                last_error_code TEXT,
                lease_id TEXT,
                lease_expires_at TEXT,
                last_attempt_at TEXT,
                failed_at TEXT,
                last_http_status INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_otlp_outbox_due ON otlp_outbox(status, next_attempt_at);

            CREATE TABLE IF NOT EXISTS otlp_asset_state (
                asset_key TEXT PRIMARY KEY,
                state_hash TEXT NOT NULL,
                last_outbox_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS otlp_export_status (
                id INTEGER PRIMARY KEY CHECK(id=1),
                dropped_events_total INTEGER NOT NULL DEFAULT 0,
                last_dropped_at TEXT,
                last_drop_reason TEXT,
                attempted_events_total INTEGER NOT NULL DEFAULT 0,
                retried_events_total INTEGER NOT NULL DEFAULT 0,
                delivered_events_total INTEGER NOT NULL DEFAULT 0,
                failed_events_total INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT,
                last_success_at TEXT,
                last_failure_at TEXT,
                last_failure_code TEXT,
                last_http_status INTEGER
            );

            INSERT OR IGNORE INTO otlp_export_status(id) VALUES(1);

            CREATE TABLE IF NOT EXISTS runtime_events (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL REFERENCES devices(id),
                observed_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                app TEXT NOT NULL,
                event_type TEXT NOT NULL,
                session_hash TEXT NOT NULL,
                agent_hash TEXT NOT NULL,
                agent_type TEXT,
                tool_name TEXT,
                mcp_server TEXT,
                model TEXT,
                status TEXT NOT NULL,
                duration_ms INTEGER,
                workspace_hash TEXT,
                user_hash TEXT,
                metadata_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_runtime_events_seen ON runtime_events(observed_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_events_session ON runtime_events(device_id, session_hash, agent_hash);

            CREATE TABLE IF NOT EXISTS inventory_sync_changes (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_key TEXT NOT NULL,
                device_id TEXT NOT NULL,
                state_hash TEXT NOT NULL,
                present INTEGER NOT NULL,
                payload_json TEXT,
                observed_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_inventory_sync_device ON inventory_sync_changes(device_id, asset_key, seq);
            CREATE INDEX IF NOT EXISTS idx_inventory_sync_asset ON inventory_sync_changes(asset_key, seq);

            CREATE VIEW IF NOT EXISTS sessions AS SELECT * FROM agent_sessions;
            CREATE VIEW IF NOT EXISTS outbox AS SELECT * FROM otlp_outbox;

            PRAGMA user_version = 4;
            COMMIT;
            "#,
        )?;

        Ok(())
    }

    /// Return list of all table names currently existing in the database.
    pub fn table_names(&self) -> Result<Vec<String>, StoreError> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name",
        )?;
        let rows = stmt.query_map([], |row| row.get(0))?;
        let mut names = Vec::new();
        for name in rows {
            names.push(name?);
        }
        Ok(names)
    }

    /// Enroll or upsert device record.
    pub fn enroll_device(&self, device: &Device) -> Result<(), StoreError> {
        let conn = self.conn.lock().unwrap();
        conn.execute(
            r#"
            INSERT INTO devices (id, hostname, os, os_version, machine, agent_version, token_hash, enrolled_at, last_seen)
            VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)
            ON CONFLICT(id) DO UPDATE SET
                hostname=excluded.hostname,
                os=excluded.os,
                os_version=excluded.os_version,
                machine=excluded.machine,
                agent_version=excluded.agent_version,
                last_seen=excluded.last_seen
            "#,
            params![
                device.id,
                device.hostname,
                device.os,
                device.os_version,
                device.machine,
                device.agent_version,
                device.token_hash,
                device.enrolled_at,
                device.last_seen,
            ],
        )?;
        Ok(())
    }

    /// Retrieve device by ID.
    pub fn get_device(&self, id: &str) -> Result<Option<Device>, StoreError> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT id, hostname, os, os_version, machine, agent_version, token_hash, enrolled_at, last_seen FROM devices WHERE id = ?1",
        )?;
        let device = stmt
            .query_row(params![id], |row| {
                Ok(Device {
                    id: row.get(0)?,
                    hostname: row.get(1)?,
                    os: row.get(2)?,
                    os_version: row.get(3)?,
                    machine: row.get(4)?,
                    agent_version: row.get(5)?,
                    token_hash: row.get(6)?,
                    enrolled_at: row.get(7)?,
                    last_seen: row.get(8)?,
                })
            })
            .optional()?;
        Ok(device)
    }

    /// List enrolled devices.
    pub fn list_devices(&self, limit: usize) -> Result<Vec<Device>, StoreError> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT id, hostname, os, os_version, machine, agent_version, token_hash, enrolled_at, last_seen FROM devices ORDER BY last_seen DESC LIMIT ?1",
        )?;
        let rows = stmt.query_map(params![limit as i64], |row| {
            Ok(Device {
                id: row.get(0)?,
                hostname: row.get(1)?,
                os: row.get(2)?,
                os_version: row.get(3)?,
                machine: row.get(4)?,
                agent_version: row.get(5)?,
                token_hash: row.get(6)?,
                enrolled_at: row.get(7)?,
                last_seen: row.get(8)?,
            })
        })?;
        let mut devices = Vec::new();
        for dev in rows {
            devices.push(dev?);
        }
        Ok(devices)
    }

    /// Upsert an asset observation for a device.
    pub fn upsert_asset(
        &self,
        device_id: &str,
        asset: &Asset,
        observed_at: &str,
    ) -> Result<(), StoreError> {
        let conn = self.conn.lock().unwrap();
        let metadata_json = serde_json::to_string(&asset.metadata)?;
        let running_int = if asset.running { 1 } else { 0 };
        let present_int = asset.present.map(|p| if p { 1 } else { 0 }).unwrap_or(1);

        conn.execute(
            r#"
            INSERT INTO assets (
                device_id, fingerprint, kind, name, vendor, version,
                path_hash, command_hash, binary_sha256, binary_fingerprint_status,
                fingerprint_library_version, metadata_json, running, first_seen, last_seen, present
            ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16)
            ON CONFLICT(device_id, fingerprint) DO UPDATE SET
                kind = excluded.kind,
                name = excluded.name,
                vendor = excluded.vendor,
                version = excluded.version,
                path_hash = excluded.path_hash,
                command_hash = excluded.command_hash,
                binary_sha256 = excluded.binary_sha256,
                binary_fingerprint_status = excluded.binary_fingerprint_status,
                fingerprint_library_version = excluded.fingerprint_library_version,
                metadata_json = excluded.metadata_json,
                running = excluded.running,
                present = excluded.present,
                last_seen = excluded.last_seen
            "#,
            params![
                device_id,
                asset.fingerprint,
                asset.kind,
                asset.name,
                asset.vendor,
                asset.version,
                asset.path_hash,
                asset.command_hash,
                asset.binary_sha256,
                asset.binary_fingerprint_status,
                asset.fingerprint_library_version,
                metadata_json,
                running_int,
                observed_at,
                observed_at,
                present_int,
            ],
        )?;
        Ok(())
    }

    /// Get asset by device ID and fingerprint.
    pub fn get_asset(
        &self,
        device_id: &str,
        fingerprint: &str,
    ) -> Result<Option<Asset>, StoreError> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            r#"
            SELECT fingerprint, kind, name, vendor, running, present, version,
                   path_hash, command_hash, binary_sha256, binary_fingerprint_status,
                   fingerprint_library_version, metadata_json, first_seen, last_seen
            FROM assets WHERE device_id = ?1 AND fingerprint = ?2
            "#,
        )?;
        let asset = stmt
            .query_row(params![device_id, fingerprint], |row| {
                let running_int: i32 = row.get(4)?;
                let present_int: Option<i32> = row.get(5)?;
                let metadata_str: String = row.get(12)?;
                let metadata = serde_json::from_str(&metadata_str).unwrap_or_default();

                Ok(Asset {
                    fingerprint: row.get(0)?,
                    kind: row.get(1)?,
                    name: row.get(2)?,
                    vendor: row.get(3)?,
                    running: running_int != 0,
                    present: present_int.map(|p| p != 0),
                    version: row.get(6)?,
                    path_hash: row.get(7)?,
                    command_hash: row.get(8)?,
                    binary_sha256: row.get(9)?,
                    binary_fingerprint_status: row.get(10)?,
                    fingerprint_library_version: row.get(11)?,
                    metadata,
                    first_seen: row.get(13)?,
                    last_seen: row.get(14)?,
                })
            })
            .optional()?;
        Ok(asset)
    }

    /// List assets with optional filtering.
    pub fn list_assets(
        &self,
        device_id: Option<&str>,
        kind: Option<&str>,
        running_only: bool,
        limit: usize,
    ) -> Result<Vec<Asset>, StoreError> {
        let conn = self.conn.lock().unwrap();
        let mut sql = String::from(
            r#"
            SELECT fingerprint, kind, name, vendor, running, present, version,
                   path_hash, command_hash, binary_sha256, binary_fingerprint_status,
                   fingerprint_library_version, metadata_json, first_seen, last_seen
            FROM assets WHERE 1=1
            "#,
        );
        let mut params_vec: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();

        if let Some(did) = device_id {
            sql.push_str(" AND device_id = ?");
            params_vec.push(Box::new(did.to_string()));
        }
        if let Some(k) = kind {
            sql.push_str(" AND kind = ?");
            params_vec.push(Box::new(k.to_string()));
        }
        if running_only {
            sql.push_str(" AND running = 1");
        }

        sql.push_str(" ORDER BY last_seen DESC LIMIT ?");
        params_vec.push(Box::new(limit as i64));

        let params_slice: Vec<&dyn rusqlite::ToSql> =
            params_vec.iter().map(|b| b.as_ref()).collect();

        let mut stmt = conn.prepare(&sql)?;
        let rows = stmt.query_map(&params_slice[..], |row| {
            let running_int: i32 = row.get(4)?;
            let present_int: Option<i32> = row.get(5)?;
            let metadata_str: String = row.get(12)?;
            let metadata = serde_json::from_str(&metadata_str).unwrap_or_default();

            Ok(Asset {
                fingerprint: row.get(0)?,
                kind: row.get(1)?,
                name: row.get(2)?,
                vendor: row.get(3)?,
                running: running_int != 0,
                present: present_int.map(|p| p != 0),
                version: row.get(6)?,
                path_hash: row.get(7)?,
                command_hash: row.get(8)?,
                binary_sha256: row.get(9)?,
                binary_fingerprint_status: row.get(10)?,
                fingerprint_library_version: row.get(11)?,
                metadata,
                first_seen: row.get(13)?,
                last_seen: row.get(14)?,
            })
        })?;

        let mut assets = Vec::new();
        for a in rows {
            assets.push(a?);
        }
        Ok(assets)
    }

    /// Upsert agent session record.
    pub fn upsert_session(&self, session: &Session) -> Result<(), StoreError> {
        let conn = self.conn.lock().unwrap();
        conn.execute(
            r#"
            INSERT INTO agent_sessions (
                device_id, session_hash, agent_hash, app, agent_type, model, status,
                workspace_hash, user_hash, first_seen, last_seen, last_event,
                event_count, tool_count, mcp_count, duration_ms
            ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16)
            ON CONFLICT(device_id, session_hash, agent_hash) DO UPDATE SET
                app = excluded.app,
                agent_type = COALESCE(excluded.agent_type, agent_sessions.agent_type),
                model = COALESCE(excluded.model, agent_sessions.model),
                status = excluded.status,
                workspace_hash = COALESCE(excluded.workspace_hash, agent_sessions.workspace_hash),
                user_hash = COALESCE(excluded.user_hash, agent_sessions.user_hash),
                last_seen = excluded.last_seen,
                last_event = excluded.last_event,
                event_count = agent_sessions.event_count + 1,
                tool_count = agent_sessions.tool_count + excluded.tool_count,
                mcp_count = agent_sessions.mcp_count + excluded.mcp_count,
                duration_ms = COALESCE(excluded.duration_ms, agent_sessions.duration_ms)
            "#,
            params![
                session.device_id,
                session.session_hash,
                session.agent_hash,
                session.app,
                session.agent_type,
                session.model,
                session.status,
                session.workspace_hash,
                session.user_hash,
                session.first_seen,
                session.last_seen,
                session.last_event,
                session.event_count,
                session.tool_count,
                session.mcp_count,
                session.duration_ms,
            ],
        )?;
        Ok(())
    }

    /// Retrieve session by composite key.
    pub fn get_session(
        &self,
        device_id: &str,
        session_hash: &str,
        agent_hash: &str,
    ) -> Result<Option<Session>, StoreError> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            r#"
            SELECT device_id, session_hash, agent_hash, app, agent_type, model, status,
                   workspace_hash, user_hash, first_seen, last_seen, last_event,
                   event_count, tool_count, mcp_count, duration_ms
            FROM agent_sessions
            WHERE device_id = ?1 AND session_hash = ?2 AND agent_hash = ?3
            "#,
        )?;
        let session = stmt
            .query_row(params![device_id, session_hash, agent_hash], |row| {
                Ok(Session {
                    device_id: row.get(0)?,
                    session_hash: row.get(1)?,
                    agent_hash: row.get(2)?,
                    app: row.get(3)?,
                    agent_type: row.get(4)?,
                    model: row.get(5)?,
                    status: row.get(6)?,
                    workspace_hash: row.get(7)?,
                    user_hash: row.get(8)?,
                    first_seen: row.get(9)?,
                    last_seen: row.get(10)?,
                    last_event: row.get(11)?,
                    event_count: row.get(12)?,
                    tool_count: row.get(13)?,
                    mcp_count: row.get(14)?,
                    duration_ms: row.get(15)?,
                })
            })
            .optional()?;
        Ok(session)
    }

    /// List agent sessions.
    pub fn list_sessions(
        &self,
        device_id: Option<&str>,
        status: Option<&str>,
        limit: usize,
    ) -> Result<Vec<Session>, StoreError> {
        let conn = self.conn.lock().unwrap();
        let mut sql = String::from(
            r#"
            SELECT device_id, session_hash, agent_hash, app, agent_type, model, status,
                   workspace_hash, user_hash, first_seen, last_seen, last_event,
                   event_count, tool_count, mcp_count, duration_ms
            FROM agent_sessions WHERE 1=1
            "#,
        );
        let mut params_vec: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();

        if let Some(did) = device_id {
            sql.push_str(" AND device_id = ?");
            params_vec.push(Box::new(did.to_string()));
        }
        if let Some(st) = status {
            sql.push_str(" AND status = ?");
            params_vec.push(Box::new(st.to_string()));
        }

        sql.push_str(" ORDER BY last_seen DESC LIMIT ?");
        params_vec.push(Box::new(limit as i64));

        let params_slice: Vec<&dyn rusqlite::ToSql> =
            params_vec.iter().map(|b| b.as_ref()).collect();

        let mut stmt = conn.prepare(&sql)?;
        let rows = stmt.query_map(&params_slice[..], |row| {
            Ok(Session {
                device_id: row.get(0)?,
                session_hash: row.get(1)?,
                agent_hash: row.get(2)?,
                app: row.get(3)?,
                agent_type: row.get(4)?,
                model: row.get(5)?,
                status: row.get(6)?,
                workspace_hash: row.get(7)?,
                user_hash: row.get(8)?,
                first_seen: row.get(9)?,
                last_seen: row.get(10)?,
                last_event: row.get(11)?,
                event_count: row.get(12)?,
                tool_count: row.get(13)?,
                mcp_count: row.get(14)?,
                duration_ms: row.get(15)?,
            })
        })?;

        let mut sessions = Vec::new();
        for s in rows {
            sessions.push(s?);
        }
        Ok(sessions)
    }

    /// Insert record into OTLP outbox.
    pub fn insert_outbox(&self, record: &OutboxRecord) -> Result<(), StoreError> {
        let conn = self.conn.lock().unwrap();
        conn.execute(
            r#"
            INSERT INTO otlp_outbox (
                id, asset_key, payload_json, payload_bytes, status, attempt_count,
                next_attempt_at, created_at, delivered_at, last_error_code, lease_id,
                lease_expires_at, last_attempt_at, failed_at, last_http_status
            ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15)
            "#,
            params![
                record.id,
                record.asset_key,
                record.payload_json,
                record.payload_bytes,
                record.status,
                record.attempt_count,
                record.next_attempt_at,
                record.created_at,
                record.delivered_at,
                record.last_error_code,
                record.lease_id,
                record.lease_expires_at,
                record.last_attempt_at,
                record.failed_at,
                record.last_http_status,
            ],
        )?;
        Ok(())
    }

    /// Retrieve outbox record by ID.
    pub fn get_outbox(&self, id: &str) -> Result<Option<OutboxRecord>, StoreError> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            r#"
            SELECT id, asset_key, payload_json, payload_bytes, status, attempt_count,
                   next_attempt_at, created_at, delivered_at, last_error_code, lease_id,
                   lease_expires_at, last_attempt_at, failed_at, last_http_status
            FROM otlp_outbox WHERE id = ?1
            "#,
        )?;
        let record = stmt
            .query_row(params![id], |row| {
                Ok(OutboxRecord {
                    id: row.get(0)?,
                    asset_key: row.get(1)?,
                    payload_json: row.get(2)?,
                    payload_bytes: row.get(3)?,
                    status: row.get(4)?,
                    attempt_count: row.get(5)?,
                    next_attempt_at: row.get(6)?,
                    created_at: row.get(7)?,
                    delivered_at: row.get(8)?,
                    last_error_code: row.get(9)?,
                    lease_id: row.get(10)?,
                    lease_expires_at: row.get(11)?,
                    last_attempt_at: row.get(12)?,
                    failed_at: row.get(13)?,
                    last_http_status: row.get(14)?,
                })
            })
            .optional()?;
        Ok(record)
    }

    /// List outbox records by status.
    pub fn list_outbox(
        &self,
        status: Option<&str>,
        limit: usize,
    ) -> Result<Vec<OutboxRecord>, StoreError> {
        let conn = self.conn.lock().unwrap();
        let mut sql = String::from(
            r#"
            SELECT id, asset_key, payload_json, payload_bytes, status, attempt_count,
                   next_attempt_at, created_at, delivered_at, last_error_code, lease_id,
                   lease_expires_at, last_attempt_at, failed_at, last_http_status
            FROM otlp_outbox WHERE 1=1
            "#,
        );
        let mut params_vec: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();

        if let Some(st) = status {
            sql.push_str(" AND status = ?");
            params_vec.push(Box::new(st.to_string()));
        }

        sql.push_str(" ORDER BY created_at ASC, id ASC LIMIT ?");
        params_vec.push(Box::new(limit as i64));

        let params_slice: Vec<&dyn rusqlite::ToSql> =
            params_vec.iter().map(|b| b.as_ref()).collect();

        let mut stmt = conn.prepare(&sql)?;
        let rows = stmt.query_map(&params_slice[..], |row| {
            Ok(OutboxRecord {
                id: row.get(0)?,
                asset_key: row.get(1)?,
                payload_json: row.get(2)?,
                payload_bytes: row.get(3)?,
                status: row.get(4)?,
                attempt_count: row.get(5)?,
                next_attempt_at: row.get(6)?,
                created_at: row.get(7)?,
                delivered_at: row.get(8)?,
                last_error_code: row.get(9)?,
                lease_id: row.get(10)?,
                lease_expires_at: row.get(11)?,
                last_attempt_at: row.get(12)?,
                failed_at: row.get(13)?,
                last_http_status: row.get(14)?,
            })
        })?;

        let mut records = Vec::new();
        for r in rows {
            records.push(r?);
        }
        Ok(records)
    }

    /// Claim due records from the outbox under lease.
    pub fn claim_outbox(
        &self,
        batch_records: usize,
        batch_bytes: i64,
        lease_id: &str,
        lease_expires_at: &str,
        now: &str,
    ) -> Result<Vec<OutboxRecord>, StoreError> {
        let conn = self.conn.lock().unwrap();
        conn.execute("BEGIN IMMEDIATE", [])?;

        let mut candidates_stmt = conn.prepare(
            r#"
            SELECT id, asset_key, payload_json, payload_bytes, status, attempt_count,
                   next_attempt_at, created_at, delivered_at, last_error_code, lease_id,
                   lease_expires_at, last_attempt_at, failed_at, last_http_status
            FROM otlp_outbox
            WHERE status IN ('pending', 'retry') AND next_attempt_at <= ?1
            ORDER BY created_at ASC, id ASC
            LIMIT ?2
            "#,
        )?;

        let candidate_rows =
            candidates_stmt.query_map(params![now, batch_records as i64], |row| {
                Ok(OutboxRecord {
                    id: row.get(0)?,
                    asset_key: row.get(1)?,
                    payload_json: row.get(2)?,
                    payload_bytes: row.get(3)?,
                    status: row.get(4)?,
                    attempt_count: row.get(5)?,
                    next_attempt_at: row.get(6)?,
                    created_at: row.get(7)?,
                    delivered_at: row.get(8)?,
                    last_error_code: row.get(9)?,
                    lease_id: row.get(10)?,
                    lease_expires_at: row.get(11)?,
                    last_attempt_at: row.get(12)?,
                    failed_at: row.get(13)?,
                    last_http_status: row.get(14)?,
                })
            })?;

        let mut claimed = Vec::new();
        let mut total_bytes = 0i64;

        for r in candidate_rows {
            let mut record = r?;
            if !claimed.is_empty() && total_bytes + record.payload_bytes > batch_bytes {
                break;
            }
            total_bytes += record.payload_bytes;
            record.status = "sending".to_string();
            record.lease_id = Some(lease_id.to_string());
            record.lease_expires_at = Some(lease_expires_at.to_string());
            claimed.push(record);
        }

        for r in &claimed {
            conn.execute(
                "UPDATE otlp_outbox SET status = 'sending', lease_id = ?1, lease_expires_at = ?2 WHERE id = ?3",
                params![lease_id, lease_expires_at, r.id],
            )?;
        }

        conn.execute("COMMIT", [])?;
        Ok(claimed)
    }

    /// Mark outbox records as delivered, failed, or retry.
    pub fn finish_outbox(
        &self,
        ids: &[&str],
        status: &str,
        error_code: Option<&str>,
        http_status: Option<i64>,
        now: &str,
    ) -> Result<usize, StoreError> {
        let conn = self.conn.lock().unwrap();
        conn.execute("BEGIN IMMEDIATE", [])?;

        let mut updated_count = 0;
        for &id in ids {
            let delivered_at = if status == "delivered" {
                Some(now)
            } else {
                None
            };
            let failed_at = if status == "failed" { Some(now) } else { None };

            let count = conn.execute(
                r#"
                UPDATE otlp_outbox
                SET status = ?1, last_error_code = ?2, last_http_status = ?3,
                    delivered_at = ?4, failed_at = ?5, lease_id = NULL, lease_expires_at = NULL
                WHERE id = ?6
                "#,
                params![status, error_code, http_status, delivered_at, failed_at, id],
            )?;
            updated_count += count;
        }

        conn.execute("COMMIT", [])?;
        Ok(updated_count)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_in_memory_store_initialization() {
        let store = Store::open_in_memory().expect("open in memory store");
        let tables = store.table_names().expect("get tables");

        assert!(tables.contains(&"devices".to_string()));
        assert!(tables.contains(&"assets".to_string()));
        assert!(tables.contains(&"agent_sessions".to_string()));
        assert!(tables.contains(&"sessions".to_string()));
        assert!(tables.contains(&"otlp_outbox".to_string()));
        assert!(tables.contains(&"outbox".to_string()));
    }

    #[test]
    fn test_device_crud() {
        let store = Store::open_in_memory().expect("open store");
        let device = Device::new(
            "dev-001",
            "host-01",
            "macOS",
            Some("15.1".into()),
            Some("arm64".into()),
            Some("0.1.0".into()),
            "token_hash_001",
            "2026-09-21T00:00:00Z",
            "2026-09-21T00:00:00Z",
        );

        store.enroll_device(&device).expect("enroll device");
        let fetched = store.get_device("dev-001").expect("get device");
        assert_eq!(fetched, Some(device.clone()));

        let devices = store.list_devices(10).expect("list devices");
        assert_eq!(devices.len(), 1);
        assert_eq!(devices[0].id, "dev-001");
    }

    #[test]
    fn test_asset_crud() {
        let store = Store::open_in_memory().expect("open store");
        let device = Device::new(
            "dev-001",
            "host-01",
            "macOS",
            None,
            None,
            None,
            "token_hash_001",
            "2026-09-21T00:00:00Z",
            "2026-09-21T00:00:00Z",
        );
        store.enroll_device(&device).expect("enroll device");

        let mut asset = Asset::new("fp-001", "application", "Cursor", "Anysphere", true);
        asset.version = Some("0.42.0".into());
        asset
            .metadata
            .insert("executable".into(), serde_json::json!("cursor"));

        store
            .upsert_asset("dev-001", &asset, "2026-09-21T00:00:00Z")
            .expect("upsert asset");

        let fetched = store
            .get_asset("dev-001", "fp-001")
            .expect("get asset")
            .expect("asset found");
        assert_eq!(fetched.name, "Cursor");
        assert_eq!(fetched.vendor, "Anysphere");
        assert_eq!(fetched.version, Some("0.42.0".into()));
        assert!(fetched.running);

        let list = store
            .list_assets(Some("dev-001"), None, true, 10)
            .expect("list assets");
        assert_eq!(list.len(), 1);
        assert_eq!(list[0].fingerprint, "fp-001");
    }

    #[test]
    fn test_session_crud() {
        let store = Store::open_in_memory().expect("open store");
        let device = Device::new(
            "dev-001",
            "host-01",
            "macOS",
            None,
            None,
            None,
            "token_hash_001",
            "2026-09-21T00:00:00Z",
            "2026-09-21T00:00:00Z",
        );
        store.enroll_device(&device).expect("enroll device");

        let session = Session::new(
            "dev-001",
            "sess-hash-1",
            "agent-hash-1",
            "Claude Code",
            "active",
            "2026-09-21T00:00:00Z",
            "2026-09-21T00:00:00Z",
            "user_prompt",
        );
        store.upsert_session(&session).expect("upsert session");

        let fetched = store
            .get_session("dev-001", "sess-hash-1", "agent-hash-1")
            .expect("get session");
        assert_eq!(fetched.unwrap().app, "Claude Code");

        let sessions = store
            .list_sessions(Some("dev-001"), Some("active"), 10)
            .expect("list sessions");
        assert_eq!(sessions.len(), 1);
    }

    #[test]
    fn test_outbox_flow() {
        let store = Store::open_in_memory().expect("open store");
        let record = OutboxRecord::new(
            "out-1",
            "asset-key-1",
            r#"{"event":"edgedisco.asset.observed"}"#,
            38,
            "2026-09-21T00:00:00Z",
            "2026-09-21T00:00:00Z",
        );
        store.insert_outbox(&record).expect("insert outbox");

        let pending = store
            .list_outbox(Some("pending"), 10)
            .expect("list pending");
        assert_eq!(pending.len(), 1);

        let claimed = store
            .claim_outbox(
                10,
                1024 * 1024,
                "lease-123",
                "2026-09-21T00:05:00Z",
                "2026-09-21T00:00:00Z",
            )
            .expect("claim outbox");
        assert_eq!(claimed.len(), 1);
        assert_eq!(claimed[0].id, "out-1");

        let finished = store
            .finish_outbox(
                &["out-1"],
                "delivered",
                None,
                Some(200),
                "2026-09-21T00:01:00Z",
            )
            .expect("finish outbox");
        assert_eq!(finished, 1);

        let delivered = store
            .get_outbox("out-1")
            .expect("get outbox")
            .expect("record found");
        assert_eq!(delivered.status, "delivered");
        assert_eq!(delivered.delivered_at, Some("2026-09-21T00:01:00Z".into()));
    }
}
