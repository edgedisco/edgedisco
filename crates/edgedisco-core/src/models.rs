use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// An enrolled endpoint device.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Device {
    pub id: String,
    pub hostname: String,
    pub os: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub os_version: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub machine: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent_version: Option<String>,
    pub token_hash: String,
    pub enrolled_at: String,
    pub last_seen: String,
}

impl Device {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        id: impl Into<String>,
        hostname: impl Into<String>,
        os: impl Into<String>,
        os_version: Option<String>,
        machine: Option<String>,
        agent_version: Option<String>,
        token_hash: impl Into<String>,
        enrolled_at: impl Into<String>,
        last_seen: impl Into<String>,
    ) -> Self {
        Self {
            id: id.into(),
            hostname: hostname.into(),
            os: os.into(),
            os_version,
            machine,
            agent_version,
            token_hash: token_hash.into(),
            enrolled_at: enrolled_at.into(),
            last_seen: last_seen.into(),
        }
    }
}

/// Discovered AI model, developer tool, MCP server, or agent runtime asset.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Asset {
    pub fingerprint: String,
    pub kind: String,
    pub name: String,
    pub vendor: String,
    pub running: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub present: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub version: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub path_hash: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub command_hash: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub binary_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub binary_fingerprint_status: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fingerprint_library_version: Option<String>,
    #[serde(default)]
    pub metadata: HashMap<String, serde_json::Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub first_seen: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_seen: Option<String>,
}

impl Asset {
    pub fn new(
        fingerprint: impl Into<String>,
        kind: impl Into<String>,
        name: impl Into<String>,
        vendor: impl Into<String>,
        running: bool,
    ) -> Self {
        Self {
            fingerprint: fingerprint.into(),
            kind: kind.into(),
            name: name.into(),
            vendor: vendor.into(),
            running,
            present: Some(true),
            version: None,
            path_hash: None,
            command_hash: None,
            binary_sha256: None,
            binary_fingerprint_status: None,
            fingerprint_library_version: None,
            metadata: HashMap::new(),
            first_seen: None,
            last_seen: None,
        }
    }
}

/// Active or historical AI agent execution session.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Session {
    pub device_id: String,
    pub session_hash: String,
    pub agent_hash: String,
    pub app: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent_type: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    pub status: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub workspace_hash: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub user_hash: Option<String>,
    pub first_seen: String,
    pub last_seen: String,
    pub last_event: String,
    #[serde(default)]
    pub event_count: i64,
    #[serde(default)]
    pub tool_count: i64,
    #[serde(default)]
    pub mcp_count: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub duration_ms: Option<i64>,
}

pub type AgentSession = Session;

impl Session {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        device_id: impl Into<String>,
        session_hash: impl Into<String>,
        agent_hash: impl Into<String>,
        app: impl Into<String>,
        status: impl Into<String>,
        first_seen: impl Into<String>,
        last_seen: impl Into<String>,
        last_event: impl Into<String>,
    ) -> Self {
        Self {
            device_id: device_id.into(),
            session_hash: session_hash.into(),
            agent_hash: agent_hash.into(),
            app: app.into(),
            agent_type: None,
            model: None,
            status: status.into(),
            workspace_hash: None,
            user_hash: None,
            first_seen: first_seen.into(),
            last_seen: last_seen.into(),
            last_event: last_event.into(),
            event_count: 1,
            tool_count: 0,
            mcp_count: 0,
            duration_ms: None,
        }
    }
}

/// Transactional outbox record for telemetry/OTLP export.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OutboxRecord {
    pub id: String,
    pub asset_key: String,
    pub payload_json: String,
    pub payload_bytes: i64,
    pub status: String,
    #[serde(default)]
    pub attempt_count: i64,
    pub next_attempt_at: String,
    pub created_at: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delivered_at: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_error_code: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub lease_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub lease_expires_at: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_attempt_at: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub failed_at: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_http_status: Option<i64>,
}

impl OutboxRecord {
    pub fn new(
        id: impl Into<String>,
        asset_key: impl Into<String>,
        payload_json: impl Into<String>,
        payload_bytes: i64,
        next_attempt_at: impl Into<String>,
        created_at: impl Into<String>,
    ) -> Self {
        Self {
            id: id.into(),
            asset_key: asset_key.into(),
            payload_json: payload_json.into(),
            payload_bytes,
            status: "pending".to_string(),
            attempt_count: 0,
            next_attempt_at: next_attempt_at.into(),
            created_at: created_at.into(),
            delivered_at: None,
            last_error_code: None,
            lease_id: None,
            lease_expires_at: None,
            last_attempt_at: None,
            failed_at: None,
            last_http_status: None,
        }
    }
}

/// Privacy guarantees reported by endpoint scans.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PrivacyFlags {
    #[serde(default)]
    pub content_captured: bool,
    #[serde(default)]
    pub secrets_captured: bool,
    #[serde(default = "default_true")]
    pub paths_hashed: bool,
    #[serde(default = "default_true")]
    pub command_lines_hashed: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub binary_contents_hashed: Option<bool>,
}

fn default_true() -> bool {
    true
}

impl Default for PrivacyFlags {
    fn default() -> Self {
        Self {
            content_captured: false,
            secrets_captured: false,
            paths_hashed: true,
            command_lines_hashed: true,
            binary_contents_hashed: Some(true),
        }
    }
}

/// Device properties in a scan report.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DeviceReport {
    pub hostname: String,
    pub os: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub os_version: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub machine: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent_version: Option<String>,
}

/// Full endpoint scan report payload.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScanReport {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub schema_version: Option<i64>,
    pub scan_id: String,
    pub observed_at: String,
    pub device: DeviceReport,
    pub assets: Vec<Asset>,
    pub privacy: PrivacyFlags,
}

/// Endpoint runtime event observation.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RuntimeEvent {
    pub id: String,
    pub device_id: String,
    pub observed_at: String,
    pub received_at: String,
    pub app: String,
    pub event_type: String,
    pub session_hash: String,
    pub agent_hash: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub agent_type: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mcp_server: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model: Option<String>,
    pub status: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub duration_ms: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub workspace_hash: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub user_hash: Option<String>,
    #[serde(default)]
    pub metadata: HashMap<String, serde_json::Value>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_device_serialization() {
        let device = Device::new(
            "dev-123",
            "mark-mac",
            "Darwin",
            Some("15.0".into()),
            Some("arm64".into()),
            Some("0.1.0".into()),
            "token_hash_abc",
            "2026-09-21T00:00:00Z",
            "2026-09-21T00:00:00Z",
        );
        let json = serde_json::to_string(&device).expect("serialize");
        let parsed: Device = serde_json::from_str(&json).expect("deserialize");
        assert_eq!(device, parsed);
    }

    #[test]
    fn test_asset_serialization() {
        let mut asset = Asset::new("fp-123", "application", "Claude", "Anthropic", true);
        asset.version = Some("0.2.0".into());
        asset
            .metadata
            .insert("executable".into(), serde_json::json!("claude"));

        let json = serde_json::to_string(&asset).expect("serialize");
        let parsed: Asset = serde_json::from_str(&json).expect("deserialize");
        assert_eq!(asset, parsed);
    }

    #[test]
    fn test_session_serialization() {
        let session = Session::new(
            "dev-123",
            "sess-hash",
            "agent-hash",
            "Claude Code",
            "active",
            "2026-09-21T00:00:00Z",
            "2026-09-21T00:00:00Z",
            "prompt",
        );
        let json = serde_json::to_string(&session).expect("serialize");
        let parsed: Session = serde_json::from_str(&json).expect("deserialize");
        assert_eq!(session, parsed);
    }

    #[test]
    fn test_outbox_serialization() {
        let outbox = OutboxRecord::new(
            "out-123",
            "asset-key-1",
            r#"{"event":"test"}"#,
            16,
            "2026-09-21T00:00:00Z",
            "2026-09-21T00:00:00Z",
        );
        let json = serde_json::to_string(&outbox).expect("serialize");
        let parsed: OutboxRecord = serde_json::from_str(&json).expect("deserialize");
        assert_eq!(outbox, parsed);
    }
}
