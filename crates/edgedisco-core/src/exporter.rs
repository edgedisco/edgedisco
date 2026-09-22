use crate::store::{Store, StoreError};
use chrono::{DateTime, Duration as ChronoDuration, SecondsFormat, Utc};
use opentelemetry_proto::tonic::collector::logs::v1::{
    ExportLogsServiceRequest, ExportLogsServiceResponse,
};
use opentelemetry_proto::tonic::common::v1::{any_value, AnyValue, InstrumentationScope, KeyValue};
use opentelemetry_proto::tonic::logs::v1::{LogRecord, ResourceLogs, ScopeLogs, SeverityNumber};
use opentelemetry_proto::tonic::resource::v1::Resource;
use prost::Message;
use reqwest::{Client, Url};
use serde_json::{json, Map, Value};
use std::collections::HashSet;
use std::time::Duration;
use thiserror::Error;
use uuid::Uuid;

const ASSET_EVENT_NAME: &str = "edgedisco.asset.observed";
const INVENTORY_EVENT_NAME: &str = "edgedisco.device.inventory";
const OTLP_CONTENT_TYPE: &str = "application/x-protobuf";
const MAX_RESPONSE_BYTES: usize = 4 * 1024 * 1024;

#[derive(Debug, Clone)]
pub struct ExporterConfig {
    pub endpoint: String,
    pub batch_records: usize,
    pub batch_bytes: i64,
    pub initial_backoff: Duration,
    pub max_backoff: Duration,
    pub request_timeout: Duration,
}

impl ExporterConfig {
    pub fn for_endpoint(endpoint: impl Into<String>) -> Self {
        Self {
            endpoint: endpoint.into(),
            batch_records: 100,
            batch_bytes: 512 * 1024,
            initial_backoff: Duration::from_secs(1),
            max_backoff: Duration::from_secs(300),
            request_timeout: Duration::from_secs(10),
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

#[derive(Debug, Clone)]
pub struct OtlpExporter {
    config: ExporterConfig,
    client: Client,
}

pub fn retry_delay(initial: Duration, maximum: Duration, attempt_count: u32) -> Duration {
    let factor = 1_u32.checked_shl(attempt_count.min(31)).unwrap_or(u32::MAX);
    initial.saturating_mul(factor).min(maximum)
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
    pub fn new(config: ExporterConfig) -> Result<Self, ExportError> {
        if config.batch_records == 0 || config.batch_bytes <= 0 {
            return Err(ExportError::Config(
                "batch record and byte limits must be positive".into(),
            ));
        }
        let endpoint = Url::parse(&config.endpoint)
            .map_err(|error| ExportError::Config(format!("invalid endpoint: {error}")))?;
        let loopback_http = endpoint.scheme() == "http"
            && endpoint.host_str().is_some_and(|host| {
                host == "localhost"
                    || host
                        .parse::<std::net::IpAddr>()
                        .is_ok_and(|ip| ip.is_loopback())
            });
        if endpoint.scheme() != "https" && !loopback_http {
            return Err(ExportError::Config(
                "endpoint must use HTTPS (HTTP is permitted only for loopback tests)".into(),
            ));
        }
        let client = Client::builder()
            .https_only(!loopback_http)
            .redirect(reqwest::redirect::Policy::none())
            .no_proxy()
            .timeout(config.request_timeout)
            .build()?;
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

        match self
            .client
            .post(&self.config.endpoint)
            .header(reqwest::header::CONTENT_TYPE, OTLP_CONTENT_TYPE)
            .body(payload)
            .send()
            .await
        {
            Ok(mut response) => {
                let status = response.status().as_u16();
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
                    let retried =
                        store.retry_outbox(&ids, "transport", Some(status.into()), now, &next)?;
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
                        .as_secs(),
                    )?;
                    let retried = store.retry_outbox(
                        &ids,
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
                let retried = store.retry_outbox(&ids, "transport", None, now, &next)?;
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
}
