//! Persistent operator settings. Loading never rewrites the original file.
use crate::cli::DaemonArgs;
use edgedisco_core::exporter::{ExporterConfig, OtlpExporter};
use serde::Deserialize;
use std::path::PathBuf;
use std::time::Duration;

pub async fn check_ready(path: &std::path::Path) -> Result<(), Box<dyn std::error::Error>> {
    use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
    use tokio::net::UnixStream;
    use tokio::time::{sleep, timeout, Duration};
    timeout(Duration::from_secs(30), async {
        loop {
            let attempt = async {
                let mut stream = UnixStream::connect(path).await?;
                stream.write_all(b"{\"protocol_version\":1,\"request_id\":\"readiness\",\"method\":\"status\"}\n").await?;
                let mut frame = Vec::new();
                BufReader::new(stream).take(65536).read_until(b'\n', &mut frame).await?;
                let response: serde_json::Value = serde_json::from_slice(&frame)?;
                Ok::<bool, Box<dyn std::error::Error>>(
                    frame.last() == Some(&b'\n') && response["protocol_version"] == 1
                    && response["request_id"] == "readiness" && response["ok"] == true
                    && response["result"]["healthy"] == true
                )
            };
            if matches!(timeout(Duration::from_secs(2), attempt).await, Ok(Ok(true))) {
                return;
            }
            sleep(Duration::from_millis(250)).await;
        }
    }).await.map_err(|_| "daemon did not answer IPC status within 30 seconds")?;
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Settings {
    schema_version: u32,
    interval_seconds: Option<u64>,
    otlp_endpoint: Option<String>,
    otlp_batch_size: Option<usize>,
    otlp_headers: Option<String>,
    otlp_compression: Option<String>,
    otlp_timeout_ms: Option<u64>,
    otlp_ca_certificate: Option<PathBuf>,
    otlp_client_certificate: Option<PathBuf>,
    otlp_client_key: Option<PathBuf>,
}

pub fn resolve(args: &DaemonArgs) -> Result<DaemonArgs, Box<dyn std::error::Error>> {
    let mut resolved = args.clone();
    let mut transport_settings = None;
    if let Some(path) = &args.config {
        match std::fs::symlink_metadata(path) {
            Ok(metadata) if !metadata.is_file() => {
                return Err("configuration must be a regular file, not a symlink".into());
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
        match std::fs::read(path) {
            Ok(bytes) => {
                let settings: Settings = serde_json::from_slice(&bytes)
                    .map_err(|_| "invalid daemon configuration JSON")?;
                if settings.schema_version != 1 {
                    return Err("unsupported configuration schema_version; expected 1".into());
                }
                if settings.otlp_headers.is_some() {
                    #[cfg(unix)]
                    {
                        use std::os::unix::fs::PermissionsExt;
                        let mode = std::fs::metadata(path)?.permissions().mode();
                        if mode & 0o077 != 0 {
                            return Err(
                                "OTLP headers require a private daemon configuration file".into()
                            );
                        }
                    }
                }
                if let Some(interval) = settings.interval_seconds {
                    resolved.interval = interval;
                }
                if let Some(batch) = settings.otlp_batch_size {
                    resolved.otlp_batch_size = batch;
                }
                if let Some(endpoint) = &settings.otlp_endpoint {
                    resolved.otlp_endpoint = Some(endpoint.clone());
                }
                transport_settings = Some(settings);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
    }
    if resolved.interval == 0 || resolved.otlp_batch_size == 0 {
        return Err("interval and OTLP batch size must be greater than zero".into());
    }
    let mut config = resolved
        .otlp_endpoint
        .as_ref()
        .map(ExporterConfig::for_endpoint);
    if let Some(settings) = transport_settings {
        let transport_configured = settings.otlp_headers.is_some()
            || settings.otlp_compression.is_some()
            || settings.otlp_timeout_ms.is_some()
            || settings.otlp_ca_certificate.is_some()
            || settings.otlp_client_certificate.is_some()
            || settings.otlp_client_key.is_some();
        if transport_configured && config.is_none() {
            return Err("OTLP transport settings require otlp_endpoint".into());
        }
        if let Some(config) = config.as_mut() {
            config.headers = settings.otlp_headers;
            config.compression = match settings.otlp_compression.as_deref() {
                None | Some("") => false,
                Some("gzip") => true,
                Some(_) => return Err("invalid otlp_compression".into()),
            };
            if let Some(timeout_ms) = settings.otlp_timeout_ms {
                if !(1..60_000).contains(&timeout_ms) {
                    return Err("invalid otlp_timeout_ms".into());
                }
                config.request_timeout = Duration::from_millis(timeout_ms);
            }
            config.ca_certificate = settings.otlp_ca_certificate;
            config.client_certificate = settings.otlp_client_certificate;
            config.client_key = settings.otlp_client_key;
        }
    }
    if let Some(config) = config.as_mut() {
        config.batch_records = resolved.otlp_batch_size;
    }
    resolved.otlp_exporter = config.map(OtlpExporter::new).transpose()?.map(Box::new);
    Ok(resolved)
}
