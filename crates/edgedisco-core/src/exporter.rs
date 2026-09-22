use crate::store::{Store, StoreError};
use chrono::{DateTime, Duration as ChronoDuration, SecondsFormat, Utc};
use flate2::{write::GzEncoder, Compression};
use opentelemetry_proto::tonic::collector::logs::v1::{
    ExportLogsServiceRequest, ExportLogsServiceResponse,
};
use opentelemetry_proto::tonic::common::v1::{any_value, AnyValue, InstrumentationScope, KeyValue};
use opentelemetry_proto::tonic::logs::v1::{LogRecord, ResourceLogs, ScopeLogs, SeverityNumber};
use opentelemetry_proto::tonic::resource::v1::Resource;
use prost::Message;
use reqwest::{header, Certificate, Client, Identity, Url};
use serde_json::{json, Map, Value};
use std::collections::HashSet;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::Duration;
use thiserror::Error;
use uuid::Uuid;

const ASSET_EVENT_NAME: &str = "edgedisco.asset.observed";
const INVENTORY_EVENT_NAME: &str = "edgedisco.device.inventory";
const OTLP_CONTENT_TYPE: &str = "application/x-protobuf";
const MAX_RESPONSE_BYTES: usize = 4 * 1024 * 1024;

#[derive(Clone)]
pub struct ExporterConfig {
    pub endpoint: String,
    pub batch_records: usize,
    pub batch_bytes: i64,
    pub initial_backoff: Duration,
    pub max_backoff: Duration,
    pub request_timeout: Duration,
    /// Comma-separated OTLP headers. Values are deliberately redacted in Debug output.
    pub headers: Option<String>,
    pub compression: bool,
    pub ca_certificate: Option<PathBuf>,
    pub client_certificate: Option<PathBuf>,
    pub client_key: Option<PathBuf>,
}

impl std::fmt::Debug for ExporterConfig {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ExporterConfig")
            .field("endpoint", &"<redacted>")
            .field("batch_records", &self.batch_records)
            .field("batch_bytes", &self.batch_bytes)
            .field("request_timeout", &self.request_timeout)
            .field("headers_configured", &self.headers.is_some())
            .field("compression", &self.compression)
            .field("ca_certificate_configured", &self.ca_certificate.is_some())
            .field(
                "client_certificate_configured",
                &self.client_certificate.is_some(),
            )
            .field("client_key_configured", &self.client_key.is_some())
            .finish_non_exhaustive()
    }
}

impl ExporterConfig {
    pub fn for_endpoint(endpoint: impl Into<String>) -> Self {
        Self {
            endpoint: endpoint.into(),
            batch_records: 100,
            batch_bytes: 1024 * 1024,
            initial_backoff: Duration::from_secs(1),
            max_backoff: Duration::from_secs(300),
            request_timeout: Duration::from_secs(10),
            headers: None,
            compression: false,
            ca_certificate: None,
            client_certificate: None,
            client_key: None,
        }
    }
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct ExportOutcome {
    pub claimed: usize,
    pub delivered: usize,
    pub retried: usize,
    pub failed: usize,
    pub http_status: Option<u16>,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct ConnectionTestResult {
    pub accepted: bool,
    pub status: &'static str,
    pub http_status: Option<u16>,
}

impl ConnectionTestResult {
    fn new(accepted: bool, status: &'static str, http_status: Option<u16>) -> Self {
        Self {
            accepted,
            status,
            http_status,
        }
    }
}

#[derive(Debug, Error)]
pub enum ExportError {
    #[error("invalid exporter configuration: {0}")]
    Config(String),
    #[error("invalid timestamp: {0}")]
    Timestamp(String),
    #[error("invalid outbox payload: {0}")]
    Payload(String),
    #[error(transparent)]
    Store(#[from] StoreError),
    #[error("failed to build rustls HTTP client: {0}")]
    Client(#[from] reqwest::Error),
}

#[derive(Clone)]
pub struct OtlpExporter {
    config: ExporterConfig,
    client: Client,
}

impl std::fmt::Debug for OtlpExporter {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("OtlpExporter")
            .field("config", &self.config)
            .finish_non_exhaustive()
    }
}

pub fn retry_delay(initial: Duration, maximum: Duration, attempt_count: u32) -> Duration {
    let factor = 1_u32.checked_shl(attempt_count.min(31)).unwrap_or(u32::MAX);
    initial.saturating_mul(factor).min(maximum)
}

fn retry_after_seconds(value: Option<&header::HeaderValue>, now: &str) -> u64 {
    let Some(value) = value.and_then(|value| value.to_str().ok()) else {
        return 0;
    };
    let value = value.trim();
    if !value.is_empty() && value.bytes().all(|byte| byte.is_ascii_digit()) {
        return value.parse::<u64>().unwrap_or(u64::MAX).min(300);
    }
    let Some(now) = parse_timestamp(now).ok() else {
        return 0;
    };
    chrono::DateTime::parse_from_rfc2822(value)
        .ok()
        .map(|date| date.signed_duration_since(now).num_seconds().max(0) as u64)
        .unwrap_or(0)
        .min(300)
}

fn parse_timestamp(value: &str) -> Result<DateTime<Utc>, ExportError> {
    DateTime::parse_from_rfc3339(value)
        .map(|timestamp| timestamp.with_timezone(&Utc))
        .map_err(|_| ExportError::Timestamp(value.to_string()))
}

fn add_seconds(value: &str, seconds: u64) -> Result<String, ExportError> {
    let timestamp = parse_timestamp(value)?;
    let seconds = i64::try_from(seconds).unwrap_or(i64::MAX);
    let advanced = timestamp
        .checked_add_signed(ChronoDuration::seconds(seconds))
        .ok_or_else(|| ExportError::Timestamp(value.to_string()))?;
    Ok(advanced.to_rfc3339_opts(SecondsFormat::Secs, true))
}

fn parse_headers(raw: Option<&str>) -> Result<header::HeaderMap, ExportError> {
    let mut headers = header::HeaderMap::new();
    let Some(raw) = raw else {
        return Ok(headers);
    };
    if raw.is_empty() {
        return Ok(headers);
    }
    const RESERVED: &[&str] = &[
        "content-type",
        "content-length",
        "content-encoding",
        "host",
        "connection",
        "user-agent",
        "transfer-encoding",
        "trailer",
        "te",
        "upgrade",
        "cookie",
        "proxy-authorization",
        "proxy-connection",
    ];
    for entry in raw.split(',') {
        let (name, encoded) = entry
            .trim()
            .split_once('=')
            .ok_or_else(|| ExportError::Config("invalid OTLP headers".into()))?;
        let name = name.trim().to_ascii_lowercase();
        let name = header::HeaderName::from_bytes(name.as_bytes())
            .map_err(|_| ExportError::Config("invalid OTLP headers".into()))?;
        if RESERVED.contains(&name.as_str()) || headers.contains_key(&name) {
            return Err(ExportError::Config("invalid OTLP headers".into()));
        }
        let encoded = encoded.trim().as_bytes();
        let mut decoded = Vec::with_capacity(encoded.len());
        let mut offset = 0;
        while offset < encoded.len() {
            let byte = if encoded[offset] == b'%' {
                let digits = encoded
                    .get(offset + 1..offset + 3)
                    .ok_or_else(|| ExportError::Config("invalid OTLP headers".into()))?;
                let hex = std::str::from_utf8(digits)
                    .map_err(|_| ExportError::Config("invalid OTLP headers".into()))?;
                offset += 3;
                u8::from_str_radix(hex, 16)
                    .map_err(|_| ExportError::Config("invalid OTLP headers".into()))?
            } else {
                let byte = encoded[offset];
                offset += 1;
                byte
            };
            if !(32..=126).contains(&byte) || byte == b';' {
                return Err(ExportError::Config("invalid OTLP headers".into()));
            }
            decoded.push(byte);
        }
        let value = header::HeaderValue::from_bytes(&decoded)
            .map_err(|_| ExportError::Config("invalid OTLP headers".into()))?;
        headers.insert(name, value);
    }
    Ok(headers)
}

fn read_certificate(path: &Path, setting: &str, private: bool) -> Result<Vec<u8>, ExportError> {
    let invalid = || ExportError::Config(format!("invalid {setting}"));
    let metadata = std::fs::symlink_metadata(path).map_err(|_| invalid())?;
    if !metadata.file_type().is_file() {
        return Err(invalid());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let forbidden = if private { 0o077 } else { 0o022 };
        if metadata.permissions().mode() & forbidden != 0 {
            return Err(invalid());
        }
    }
    std::fs::read(path).map_err(|_| invalid())
}

fn gzip_payload(payload: &[u8]) -> Result<Vec<u8>, ExportError> {
    let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
    encoder
        .write_all(payload)
        .and_then(|()| encoder.finish())
        .map_err(|_| ExportError::Payload("OTLP gzip encoding failed".into()))
}

fn otlp_value(value: &Value) -> Result<AnyValue, ExportError> {
    let value = match value {
        Value::Bool(value) => any_value::Value::BoolValue(*value),
        Value::Number(value) => any_value::Value::IntValue(
            value
                .as_i64()
                .ok_or_else(|| ExportError::Payload("integer attribute is out of range".into()))?,
        ),
        Value::String(value) => any_value::Value::StringValue(value.clone()),
        _ => {
            return Err(ExportError::Payload(
                "attributes must be strings, integers, or booleans".into(),
            ))
        }
    };
    Ok(AnyValue { value: Some(value) })
}

fn key_value(key: impl Into<String>, value: AnyValue) -> KeyValue {
    KeyValue {
        key: key.into(),
        value: Some(value),
        key_strindex: 0,
    }
}

fn string_value(value: impl Into<String>) -> AnyValue {
    AnyValue {
        value: Some(any_value::Value::StringValue(value.into())),
    }
}

fn timestamp_nanos(value: &str) -> Result<u64, ExportError> {
    let timestamp = parse_timestamp(value)?;
    let seconds = u64::try_from(timestamp.timestamp())
        .map_err(|_| ExportError::Timestamp(value.to_string()))?;
    let nanos = seconds
        .checked_mul(1_000_000_000)
        .and_then(|base| base.checked_add(u64::from(timestamp.timestamp_subsec_nanos())))
        .ok_or_else(|| ExportError::Timestamp(value.to_string()))?;
    if nanos == 0 {
        return Err(ExportError::Timestamp(value.to_string()));
    }
    Ok(nanos)
}

fn exact_keys(object: &Map<String, Value>, required: &[&str], optional: &[&str]) -> bool {
    let required: HashSet<&str> = required.iter().copied().collect();
    let allowed: HashSet<&str> = required
        .iter()
        .copied()
        .chain(optional.iter().copied())
        .collect();
    required.iter().all(|key| object.contains_key(*key))
        && object.keys().all(|key| allowed.contains(key.as_str()))
}

fn valid_hex(value: &str, prefix: &str, digits: usize) -> bool {
    value.strip_prefix(prefix).is_some_and(|hex| {
        hex.len() == digits
            && hex
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    })
}

fn known_asset_vendor(name: &str) -> Option<&str> {
    crate::catalog::catalog()
        .agents
        .iter()
        .find(|agent| agent.name == name)
        .map(|agent| agent.vendor.as_str())
        .or_else(|| {
            crate::catalog::APPLICATION_SIGNATURES
                .iter()
                .find(|signature| signature.name == name)
                .map(|signature| signature.vendor)
        })
}

struct ValidatedEvent {
    event_name: String,
    event_nanos: u64,
    observed_nanos: u64,
    attributes: Map<String, Value>,
}

fn validated_event(payload_json: &str) -> Result<ValidatedEvent, ExportError> {
    let event: Value = serde_json::from_str(payload_json)
        .map_err(|_| ExportError::Payload("invalid sanitized outbox payload".into()))?;
    let event = event
        .as_object()
        .ok_or_else(|| ExportError::Payload("outbox event must be an object".into()))?;
    if !exact_keys(
        event,
        &["timestamp", "event.name", "resource", "attributes"],
        &["recorded_at"],
    ) {
        return Err(ExportError::Payload(
            "unsupported outbox event fields".into(),
        ));
    }
    let event_name = event
        .get("event.name")
        .and_then(Value::as_str)
        .filter(|name| matches!(*name, ASSET_EVENT_NAME | INVENTORY_EVENT_NAME))
        .ok_or_else(|| ExportError::Payload("unsupported outbox event identity".into()))?;
    if event.get("resource") != Some(&json!({"service.name": "edgedisco"})) {
        return Err(ExportError::Payload(
            "unsupported outbox event identity".into(),
        ));
    }
    let event_time = event
        .get("timestamp")
        .and_then(Value::as_str)
        .ok_or_else(|| ExportError::Payload("timestamp is required".into()))?;
    let observed_time = event
        .get("recorded_at")
        .and_then(Value::as_str)
        .unwrap_or(event_time);
    let attributes = event
        .get("attributes")
        .and_then(Value::as_object)
        .ok_or_else(|| ExportError::Payload("attributes object is required".into()))?;
    let schema = attributes
        .get("edgedisco.schema.version")
        .and_then(Value::as_i64)
        .filter(|schema| matches!(*schema, 1 | 2))
        .ok_or_else(|| ExportError::Payload("unsupported outbox schema version".into()))?;

    if event_name == INVENTORY_EVENT_NAME {
        if schema != 2
            || !event.contains_key("recorded_at")
            || !exact_keys(
                attributes,
                &[
                    "edgedisco.schema.version",
                    "edgedisco.observation.id",
                    "device.id",
                    "inventory.asset_count",
                    "inventory.simulated_asset_count",
                ],
                &[],
            )
        {
            return Err(ExportError::Payload("unsupported heartbeat schema".into()));
        }
    } else {
        let mut required = vec![
            "edgedisco.schema.version",
            "edgedisco.observation.id",
            "device.id",
            "asset.kind",
            "asset.name",
            "asset.vendor",
            "asset.running",
            "edgedisco.simulated",
        ];
        if schema == 2 {
            required.push("asset.present");
        }
        if !exact_keys(
            attributes,
            &required,
            &["asset.host_app", "asset.relationship", "asset.version"],
        ) {
            return Err(ExportError::Payload("unsupported outbox attributes".into()));
        }
    }

    let observation_id = attributes
        .get("edgedisco.observation.id")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let device_id = attributes
        .get("device.id")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if !valid_hex(observation_id, "sha256:", 64) {
        return Err(ExportError::Payload("invalid observation ID".into()));
    }
    if !valid_hex(device_id, "", 32) {
        return Err(ExportError::Payload("invalid device ID".into()));
    }

    if event_name == INVENTORY_EVENT_NAME {
        let count = attributes
            .get("inventory.asset_count")
            .and_then(Value::as_i64)
            .ok_or_else(|| ExportError::Payload("invalid inventory counts".into()))?;
        let simulated = attributes
            .get("inventory.simulated_asset_count")
            .and_then(Value::as_i64)
            .ok_or_else(|| ExportError::Payload("invalid inventory counts".into()))?;
        if !(0..=10_000).contains(&count) || !(0..=count).contains(&simulated) {
            return Err(ExportError::Payload("invalid inventory counts".into()));
        }
    } else {
        let kind = attributes
            .get("asset.kind")
            .and_then(Value::as_str)
            .filter(|kind| matches!(*kind, "application" | "process" | "agent_runtime"))
            .ok_or_else(|| ExportError::Payload("invalid asset kind".into()))?;
        let name = attributes
            .get("asset.name")
            .and_then(Value::as_str)
            .ok_or_else(|| ExportError::Payload("invalid asset signature".into()))?;
        let vendor = attributes.get("asset.vendor").and_then(Value::as_str);
        if known_asset_vendor(name) != vendor {
            return Err(ExportError::Payload("invalid asset signature".into()));
        }
        let running = attributes
            .get("asset.running")
            .and_then(Value::as_bool)
            .ok_or_else(|| ExportError::Payload("invalid asset flags".into()))?;
        attributes
            .get("edgedisco.simulated")
            .and_then(Value::as_bool)
            .ok_or_else(|| ExportError::Payload("invalid asset flags".into()))?;
        if schema == 2 {
            let present = attributes
                .get("asset.present")
                .and_then(Value::as_bool)
                .ok_or_else(|| ExportError::Payload("invalid asset presence".into()))?;
            if running && !present {
                return Err(ExportError::Payload("invalid asset presence".into()));
            }
        }
        if let Some(version) = attributes.get("asset.version") {
            let version = version
                .as_str()
                .filter(|value| !value.is_empty() && value.len() <= 128)
                .ok_or_else(|| ExportError::Payload("invalid asset version".into()))?;
            let _ = version;
        }
        if kind == "agent_runtime" {
            let host = attributes
                .get("asset.host_app")
                .and_then(Value::as_str)
                .filter(|host| *host == "Direct/local" || crate::catalog::is_host_app(host))
                .ok_or_else(|| ExportError::Payload("invalid host application".into()))?;
            let expected = if host == "Direct/local" {
                "local_process"
            } else {
                "spawned_by"
            };
            if attributes.get("asset.relationship").and_then(Value::as_str) != Some(expected) {
                return Err(ExportError::Payload("invalid relationship".into()));
            }
        } else if attributes.contains_key("asset.host_app")
            || attributes.contains_key("asset.relationship")
        {
            return Err(ExportError::Payload("unexpected runtime attributes".into()));
        }
    }

    Ok(ValidatedEvent {
        event_name: event_name.to_string(),
        event_nanos: timestamp_nanos(event_time)?,
        observed_nanos: timestamp_nanos(observed_time)?,
        attributes: attributes.clone(),
    })
}

fn otlp_log_record(record: &crate::models::OutboxRecord) -> Result<LogRecord, ExportError> {
    let event = validated_event(&record.payload_json)?;
    let mut attributes = event.attributes.into_iter().collect::<Vec<_>>();
    attributes.sort_by(|left, right| left.0.cmp(&right.0));
    let attributes = attributes
        .into_iter()
        .map(|(key, value)| Ok(key_value(key, otlp_value(&value)?)))
        .collect::<Result<Vec<_>, ExportError>>()?;
    let body =
        (event.event_name == INVENTORY_EVENT_NAME).then(|| string_value(INVENTORY_EVENT_NAME));
    Ok(LogRecord {
        time_unix_nano: event.event_nanos,
        observed_time_unix_nano: event.observed_nanos,
        severity_number: SeverityNumber::Info as i32,
        severity_text: "INFO".into(),
        body,
        attributes,
        event_name: event.event_name,
        ..Default::default()
    })
}

fn resource_logs(log_record: LogRecord) -> ResourceLogs {
    ResourceLogs {
        resource: Some(Resource {
            attributes: vec![
                key_value("service.name", string_value("edgedisco")),
                key_value("service.version", string_value(env!("CARGO_PKG_VERSION"))),
            ],
            ..Default::default()
        }),
        scope_logs: vec![ScopeLogs {
            scope: Some(InstrumentationScope {
                name: "edgedisco_core.exporter".into(),
                version: env!("CARGO_PKG_VERSION").into(),
                ..Default::default()
            }),
            log_records: vec![log_record],
            schema_url: String::new(),
        }],
        schema_url: String::new(),
    }
}

/// Encode validated outbox rows as an OTLP Logs binary protobuf request.
///
/// Each row retains its own ResourceLogs envelope, matching the Python exporter's
/// protobuf concatenation behavior while allowing transport batching.
pub fn encode_otlp_request(
    records: &[&crate::models::OutboxRecord],
) -> Result<Vec<u8>, ExportError> {
    let resource_logs = records
        .iter()
        .map(|record| otlp_log_record(record).map(resource_logs))
        .collect::<Result<Vec<_>, _>>()?;
    Ok(ExportLogsServiceRequest { resource_logs }.encode_to_vec())
}

fn classify_success_response(body: &[u8]) -> Result<bool, ExportError> {
    if body.len() > MAX_RESPONSE_BYTES {
        return Err(ExportError::Payload(
            "OTLP response exceeds size limit".into(),
        ));
    }
    let response = ExportLogsServiceResponse::decode(body)
        .map_err(|_| ExportError::Payload("invalid OTLP protobuf response".into()))?;
    Ok(response
        .partial_success
        .is_some_and(|partial| partial.rejected_log_records > 0))
}

impl OtlpExporter {
    /// Send an empty OTLP Logs request. This tests transport/auth/protocol only:
    /// it neither claims outbox rows nor records a delivery.
    pub async fn test_connection(&self) -> ConnectionTestResult {
        let payload = ExportLogsServiceRequest {
            resource_logs: Vec::new(),
        }
        .encode_to_vec();
        let payload = if self.config.compression {
            match gzip_payload(&payload) {
                Ok(payload) => payload,
                Err(_) => return ConnectionTestResult::new(false, "probe_error", None),
            }
        } else {
            payload
        };
        let mut request = self
            .client
            .post(&self.config.endpoint)
            .header(header::CONTENT_TYPE, OTLP_CONTENT_TYPE)
            .header(
                header::USER_AGENT,
                format!("edgedisco/{}", env!("CARGO_PKG_VERSION")),
            );
        if self.config.compression {
            request = request.header(header::CONTENT_ENCODING, "gzip");
        } else {
            request = request.header(header::CONTENT_LENGTH, "0");
        }
        let mut response = match request.body(payload).send().await {
            Ok(response) => response,
            Err(error) if error.is_timeout() => {
                return ConnectionTestResult::new(false, "timeout", None)
            }
            Err(_) => return ConnectionTestResult::new(false, "transport_error", None),
        };
        let status = response.status().as_u16();
        if !response.status().is_success() {
            return ConnectionTestResult::new(false, "http_rejected", Some(status));
        }
        const MAX_PROBE_RESPONSE: usize = 16 * 1024;
        let mut body = Vec::new();
        loop {
            match response.chunk().await {
                Ok(Some(chunk)) if body.len() + chunk.len() <= MAX_PROBE_RESPONSE => {
                    body.extend_from_slice(&chunk)
                }
                Ok(None) => break,
                _ => return ConnectionTestResult::new(false, "invalid_response", Some(status)),
            }
        }
        match classify_success_response(&body) {
            Ok(false) => ConnectionTestResult::new(true, "accepted", Some(status)),
            Ok(true) => ConnectionTestResult::new(false, "partial_rejection", Some(status)),
            Err(_) => ConnectionTestResult::new(false, "invalid_response", Some(status)),
        }
    }

    pub fn new(config: ExporterConfig) -> Result<Self, ExportError> {
        if config.batch_records == 0 || config.batch_bytes <= 0 || config.request_timeout.is_zero()
        {
            return Err(ExportError::Config(
                "batch limits and request timeout must be positive".into(),
            ));
        }
        if config
            .endpoint
            .bytes()
            .any(|byte| !(33..=126).contains(&byte) || byte == b'\\')
        {
            return Err(ExportError::Config("invalid OTLP endpoint".into()));
        }
        let endpoint = Url::parse(&config.endpoint)
            .map_err(|_| ExportError::Config("invalid OTLP endpoint".into()))?;
        if endpoint.username() != ""
            || endpoint.password().is_some()
            || endpoint.query().is_some()
            || endpoint.fragment().is_some()
            || endpoint.host_str().is_none()
            || endpoint.port() == Some(0)
        {
            return Err(ExportError::Config("invalid OTLP endpoint".into()));
        }
        let loopback_http = endpoint.scheme() == "http"
            && endpoint.host_str().is_some_and(|host| {
                host.parse::<std::net::IpAddr>()
                    .is_ok_and(|ip| ip.is_loopback())
            });
        if endpoint.scheme() != "https" && !loopback_http {
            return Err(ExportError::Config(
                "endpoint must use HTTPS or a literal loopback HTTP address".into(),
            ));
        }
        if config.client_certificate.is_some() != config.client_key.is_some() {
            return Err(ExportError::Config(
                "OTLP client certificate and key must be configured together".into(),
            ));
        }
        let headers = parse_headers(config.headers.as_deref())?;
        let mut client_builder = Client::builder()
            .https_only(!loopback_http)
            .redirect(reqwest::redirect::Policy::none())
            .no_proxy()
            .timeout(config.request_timeout)
            .default_headers(headers);
        if let Some(path) = &config.ca_certificate {
            let pem = read_certificate(path, "OTLP CA certificate", false)?;
            let certificates = Certificate::from_pem_bundle(&pem)
                .map_err(|_| ExportError::Config("invalid OTLP CA certificate".into()))?;
            if certificates.is_empty() {
                return Err(ExportError::Config("invalid OTLP CA certificate".into()));
            }
            for certificate in certificates {
                client_builder = client_builder.add_root_certificate(certificate);
            }
        }
        if let (Some(cert_path), Some(key_path)) = (&config.client_certificate, &config.client_key)
        {
            let mut pem = read_certificate(cert_path, "OTLP client certificate", false)?;
            let key = read_certificate(key_path, "OTLP client key", true)?;
            pem.push(b'\n');
            pem.extend_from_slice(&key);
            let identity = Identity::from_pem(&pem).map_err(|_| {
                ExportError::Config("invalid OTLP client certificate or key".into())
            })?;
            client_builder = client_builder.identity(identity);
        }
        let client = client_builder.build()?;
        Ok(Self { config, client })
    }

    pub async fn export_once_at(
        &self,
        store: &Store,
        now: &str,
    ) -> Result<ExportOutcome, ExportError> {
        parse_timestamp(now)?;
        let lease_expires_at = add_seconds(now, self.config.request_timeout.as_secs() + 30)?;
        let lease_id = format!("export-{}", Uuid::new_v4());
        let records = store.claim_outbox(
            self.config.batch_records,
            self.config.batch_bytes,
            &lease_id,
            &lease_expires_at,
            now,
        )?;
        if records.is_empty() {
            return Ok(ExportOutcome::default());
        }

        let mut log_records = Vec::with_capacity(records.len());
        let mut valid_records = Vec::with_capacity(records.len());
        for record in &records {
            match otlp_log_record(record) {
                Ok(log_record) => {
                    log_records.push(log_record);
                    valid_records.push(record);
                }
                Err(_) => {
                    store.finish_outbox(
                        &[record.id.as_str()],
                        &lease_id,
                        "failed",
                        Some("invalid_payload"),
                        None,
                        now,
                    )?;
                }
            }
        }
        let invalid_count = records.len() - valid_records.len();
        if log_records.is_empty() {
            return Ok(ExportOutcome {
                claimed: records.len(),
                failed: invalid_count,
                ..Default::default()
            });
        }
        let payload = ExportLogsServiceRequest {
            resource_logs: log_records.into_iter().map(resource_logs).collect(),
        }
        .encode_to_vec();
        let payload = if self.config.compression {
            gzip_payload(&payload)?
        } else {
            payload
        };
        let ids: Vec<&str> = valid_records
            .iter()
            .map(|record| record.id.as_str())
            .collect();
        let attempt_count = valid_records
            .iter()
            .map(|record| record.attempt_count)
            .max()
            .unwrap_or(0)
            .max(0) as u32;
        if !store.begin_outbox_attempt(&ids, &lease_id, now)? {
            return Ok(ExportOutcome {
                claimed: records.len(),
                failed: invalid_count,
                ..Default::default()
            });
        }

        let mut request = self
            .client
            .post(&self.config.endpoint)
            .header(header::CONTENT_TYPE, OTLP_CONTENT_TYPE)
            .header(
                header::USER_AGENT,
                format!("edgedisco/{}", env!("CARGO_PKG_VERSION")),
            );
        if self.config.compression {
            request = request.header(header::CONTENT_ENCODING, "gzip");
        }
        match request.body(payload).send().await {
            Ok(mut response) => {
                let status = response.status().as_u16();
                let retry_after =
                    retry_after_seconds(response.headers().get(header::RETRY_AFTER), now);
                let mut body = Vec::new();
                let mut response_error = response
                    .content_length()
                    .is_some_and(|length| length > MAX_RESPONSE_BYTES as u64);
                while !response_error {
                    match response.chunk().await {
                        Ok(Some(chunk)) if body.len() + chunk.len() <= MAX_RESPONSE_BYTES => {
                            body.extend_from_slice(&chunk);
                        }
                        Ok(None) => break,
                        Ok(Some(_)) | Err(_) => response_error = true,
                    }
                }
                if response_error {
                    let next = add_seconds(
                        now,
                        retry_delay(
                            self.config.initial_backoff,
                            self.config.max_backoff,
                            attempt_count,
                        )
                        .as_secs(),
                    )?;
                    let retried = store.retry_outbox(
                        &ids,
                        &lease_id,
                        "transport",
                        Some(status.into()),
                        now,
                        &next,
                    )?;
                    return Ok(ExportOutcome {
                        claimed: records.len(),
                        retried,
                        failed: invalid_count,
                        http_status: Some(status),
                        ..Default::default()
                    });
                }
                if (200..300).contains(&status) {
                    match classify_success_response(&body) {
                        Ok(false) => {
                            let delivered = store.finish_outbox(
                                &ids,
                                &lease_id,
                                "delivered",
                                None,
                                Some(status.into()),
                                now,
                            )?;
                            Ok(ExportOutcome {
                                claimed: records.len(),
                                delivered,
                                failed: invalid_count,
                                http_status: Some(status),
                                ..Default::default()
                            })
                        }
                        Ok(true) => {
                            let failed = store.finish_outbox(
                                &ids,
                                &lease_id,
                                "failed",
                                Some("partial_success"),
                                Some(status.into()),
                                now,
                            )?;
                            Ok(ExportOutcome {
                                claimed: records.len(),
                                failed: invalid_count + failed,
                                http_status: Some(status),
                                ..Default::default()
                            })
                        }
                        Err(_) => {
                            let next = add_seconds(
                                now,
                                retry_delay(
                                    self.config.initial_backoff,
                                    self.config.max_backoff,
                                    attempt_count,
                                )
                                .as_secs(),
                            )?;
                            let retried = store.retry_outbox(
                                &ids,
                                &lease_id,
                                "invalid_response",
                                Some(status.into()),
                                now,
                                &next,
                            )?;
                            Ok(ExportOutcome {
                                claimed: records.len(),
                                retried,
                                failed: invalid_count,
                                http_status: Some(status),
                                ..Default::default()
                            })
                        }
                    }
                } else if matches!(status, 429 | 502 | 503 | 504) {
                    let next = add_seconds(
                        now,
                        retry_delay(
                            self.config.initial_backoff,
                            self.config.max_backoff,
                            attempt_count,
                        )
                        .as_secs()
                        .max(retry_after),
                    )?;
                    let retried = store.retry_outbox(
                        &ids,
                        &lease_id,
                        "http_retryable",
                        Some(status.into()),
                        now,
                        &next,
                    )?;
                    Ok(ExportOutcome {
                        claimed: records.len(),
                        retried,
                        failed: invalid_count,
                        http_status: Some(status),
                        ..Default::default()
                    })
                } else {
                    let failed = store.finish_outbox(
                        &ids,
                        &lease_id,
                        "failed",
                        Some("http_permanent"),
                        Some(status.into()),
                        now,
                    )?;
                    Ok(ExportOutcome {
                        claimed: records.len(),
                        failed: invalid_count + failed,
                        http_status: Some(status),
                        ..Default::default()
                    })
                }
            }
            Err(_) => {
                let next = add_seconds(
                    now,
                    retry_delay(
                        self.config.initial_backoff,
                        self.config.max_backoff,
                        attempt_count,
                    )
                    .as_secs(),
                )?;
                let retried = store.retry_outbox(&ids, &lease_id, "transport", None, now, &next)?;
                Ok(ExportOutcome {
                    claimed: records.len(),
                    retried,
                    failed: invalid_count,
                    http_status: None,
                    ..Default::default()
                })
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use opentelemetry_proto::tonic::collector::logs::v1::ExportLogsPartialSuccess;

    #[tokio::test]
    async fn connection_probe_sends_no_records_and_redacts_rejection_body() {
        use std::io::{Read, Write};
        use std::net::TcpListener;
        for (response, accepted, status) in [
            (
                "HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n",
                true,
                "accepted",
            ),
            (
                "HTTP/1.1 401 Unauthorized\r\nContent-Length: 13\r\n\r\nSECRET-denied",
                false,
                "http_rejected",
            ),
        ] {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let address = listener.local_addr().unwrap();
            let reply = response.to_owned();
            let server = std::thread::spawn(move || {
                let (mut stream, _) = listener.accept().unwrap();
                stream
                    .set_read_timeout(Some(Duration::from_secs(3)))
                    .unwrap();
                let mut request = Vec::new();
                let mut buffer = [0u8; 1024];
                loop {
                    let count = stream.read(&mut buffer).unwrap();
                    request.extend_from_slice(&buffer[..count]);
                    if request.windows(4).any(|bytes| bytes == b"\r\n\r\n") {
                        break;
                    }
                }
                let headers = String::from_utf8_lossy(&request).to_ascii_lowercase();
                assert!(headers.starts_with("post /v1/logs http/1.1"));
                assert!(headers.contains("content-type: application/x-protobuf"));
                assert!(headers.contains("content-length: 0"));
                stream.write_all(reply.as_bytes()).unwrap();
            });
            let mut config = ExporterConfig::for_endpoint(format!("http://{address}/v1/logs"));
            config.request_timeout = Duration::from_secs(3);
            let result = OtlpExporter::new(config).unwrap().test_connection().await;
            server.join().unwrap();
            assert_eq!(result.accepted, accepted);
            assert_eq!(result.status, status);
            assert!(!serde_json::to_string(&result).unwrap().contains("SECRET"));
        }
    }

    #[test]
    fn success_response_requires_valid_protobuf_and_detects_rejections() {
        assert!(!classify_success_response(&[]).expect("empty response is valid protobuf"));
        assert!(classify_success_response(b"not protobuf").is_err());

        let response = ExportLogsServiceResponse {
            partial_success: Some(ExportLogsPartialSuccess {
                rejected_log_records: 1,
                error_message: "collector rejected a record".into(),
            }),
        }
        .encode_to_vec();
        assert!(classify_success_response(&response).expect("valid partial response"));
    }

    #[test]
    fn endpoint_and_header_validation_never_echo_credentials() {
        for endpoint in [
            "http://localhost:4318/v1/logs",
            "http://192.168.1.5:4318/v1/logs",
            "https://user:SECRET@example.org/v1/logs",
            "https://example.org/v1/logs?token=SECRET",
            "https://example.org/v1/logs#SECRET",
        ] {
            let error = OtlpExporter::new(ExporterConfig::for_endpoint(endpoint))
                .expect_err("unsafe endpoint must fail");
            assert!(!error.to_string().contains("SECRET"));
        }
        for raw in [
            "Authorization=SECRET,authorization=SECRET",
            "Host=SECRET",
            "Cookie=SECRET",
            "x-test=SECRET%0d%0aInjected",
            "x-test=SECRET%zz",
            "x-test=SECRET;metadata",
        ] {
            let mut config = ExporterConfig::for_endpoint("https://example.org/v1/logs");
            config.headers = Some(raw.into());
            let error = OtlpExporter::new(config.clone()).expect_err("unsafe header must fail");
            assert!(!error.to_string().contains("SECRET"));
            assert!(!format!("{config:?}").contains("SECRET"));
        }
    }

    #[test]
    fn retry_after_supports_seconds_and_http_dates_with_a_cap() {
        let now = "2026-09-22T00:00:00Z";
        for (value, expected) in [
            ("120", 120),
            ("600", 300),
            ("Tue, 22 Sep 2026 00:00:30 GMT", 30),
            ("Mon, 21 Sep 2026 23:59:30 GMT", 0),
            ("nonsense", 0),
        ] {
            let header = header::HeaderValue::from_str(value).unwrap();
            assert_eq!(retry_after_seconds(Some(&header), now), expected);
        }
        assert_eq!(retry_after_seconds(None, now), 0);
    }
}
