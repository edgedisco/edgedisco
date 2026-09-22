use crate::store::{Store, StoreError};
use chrono::{DateTime, Duration as ChronoDuration, SecondsFormat, Utc};
use reqwest::{Client, Url};
use serde_json::json;
use std::time::Duration;
use thiserror::Error;
use uuid::Uuid;

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
    pub http_status: Option<u16>,
}

#[derive(Debug, Error)]
pub enum ExportError {
    #[error("invalid exporter configuration: {0}")]
    Config(String),
    #[error("invalid timestamp: {0}")]
    Timestamp(String),
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

        let log_records: Vec<_> = records
            .iter()
            .map(|record| {
                json!({
                    "body": {"stringValue": record.payload_json},
                    "attributes": [
                        {"key": "edgedisco.outbox.id", "value": {"stringValue": record.id}},
                        {"key": "edgedisco.asset.key", "value": {"stringValue": record.asset_key}}
                    ]
                })
            })
            .collect();
        let payload = json!({
            "resourceLogs": [{
                "resource": {"attributes": [{
                    "key": "service.name",
                    "value": {"stringValue": "edgedisco"}
                }]},
                "scopeLogs": [{
                    "scope": {"name": "edgedisco.outbox"},
                    "logRecords": log_records
                }]
            }]
        });
        let ids: Vec<&str> = records.iter().map(|record| record.id.as_str()).collect();
        let attempt_count = records
            .iter()
            .map(|record| record.attempt_count)
            .max()
            .unwrap_or(0)
            .max(0) as u32;

        match self
            .client
            .post(&self.config.endpoint)
            .json(&payload)
            .send()
            .await
        {
            Ok(response) => {
                let status = response.status().as_u16();
                if matches!(status, 200 | 202) {
                    let delivered =
                        store.finish_outbox(&ids, "delivered", None, Some(status.into()), now)?;
                    Ok(ExportOutcome {
                        claimed: records.len(),
                        delivered,
                        retried: 0,
                        http_status: Some(status),
                    })
                } else {
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
                        store.retry_outbox(&ids, "http_status", Some(status.into()), now, &next)?;
                    Ok(ExportOutcome {
                        claimed: records.len(),
                        delivered: 0,
                        retried,
                        http_status: Some(status),
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
                    delivered: 0,
                    retried,
                    http_status: None,
                })
            }
        }
    }
}
