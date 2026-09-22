//! Persistent operator settings. Loading never rewrites the original file.
use crate::cli::DaemonArgs;
use edgedisco_core::exporter::{ExporterConfig, OtlpExporter};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;
use tokio::sync::watch;
use uuid::Uuid;

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

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Settings {
    pub schema_version: u32,
    pub interval_seconds: Option<u64>,
    pub otlp_endpoint: Option<String>,
    pub otlp_batch_size: Option<usize>,
    #[serde(default)]
    pub export_enabled: Option<bool>,
    otlp_headers: Option<String>,
    otlp_compression: Option<String>,
    otlp_timeout_ms: Option<u64>,
    otlp_ca_certificate: Option<PathBuf>,
    otlp_client_certificate: Option<PathBuf>,
    otlp_client_key: Option<PathBuf>,
}

impl Settings {
    pub fn from_args(args: &DaemonArgs) -> Self {
        Self {
            schema_version: 1,
            interval_seconds: Some(args.interval),
            otlp_endpoint: args.otlp_endpoint.clone(),
            otlp_batch_size: Some(args.otlp_batch_size),
            export_enabled: Some(args.export_enabled.unwrap_or(args.otlp_endpoint.is_some())),
            otlp_headers: None,
            otlp_compression: None,
            otlp_timeout_ms: None,
            otlp_ca_certificate: None,
            otlp_client_certificate: None,
            otlp_client_key: None,
        }
    }

    fn copy_transport_from(&mut self, previous: &Self) {
        self.otlp_headers = previous.otlp_headers.clone();
        self.otlp_compression = previous.otlp_compression.clone();
        self.otlp_timeout_ms = previous.otlp_timeout_ms;
        self.otlp_ca_certificate = previous.otlp_ca_certificate.clone();
        self.otlp_client_certificate = previous.otlp_client_certificate.clone();
        self.otlp_client_key = previous.otlp_client_key.clone();
    }

    fn has_transport(&self) -> bool {
        self.otlp_headers.is_some()
            || self.otlp_compression.is_some()
            || self.otlp_timeout_ms.is_some()
            || self.otlp_ca_certificate.is_some()
            || self.otlp_client_certificate.is_some()
            || self.otlp_client_key.is_some()
    }

    fn public_json(&self) -> serde_json::Value {
        serde_json::json!({
            "schema_version": self.schema_version,
            "interval_seconds": self.interval_seconds,
            "otlp_endpoint": self.otlp_endpoint,
            "otlp_batch_size": self.otlp_batch_size,
            "export_enabled": self.export_enabled,
        })
    }

    pub fn apply(&self, base: &DaemonArgs) -> Result<DaemonArgs, Box<dyn std::error::Error>> {
        if self.schema_version != 1 {
            return Err("unsupported configuration schema_version; expected 1".into());
        }
        let mut resolved = base.clone();
        if let Some(interval) = self.interval_seconds {
            resolved.interval = interval;
        }
        if let Some(batch) = self.otlp_batch_size {
            resolved.otlp_batch_size = batch;
        }
        if self.export_enabled.is_some() {
            resolved.otlp_endpoint = self.otlp_endpoint.clone();
        } else if let Some(endpoint) = &self.otlp_endpoint {
            resolved.otlp_endpoint = Some(endpoint.clone());
        }
        resolved.export_enabled = Some(
            self.export_enabled
                .unwrap_or(resolved.otlp_endpoint.is_some()),
        );
        if resolved.interval == 0 || resolved.otlp_batch_size == 0 {
            return Err("interval and OTLP batch size must be greater than zero".into());
        }
        if resolved.export_enabled == Some(true) && resolved.otlp_endpoint.is_none() {
            return Err("OTLP endpoint is required when export is enabled".into());
        }
        if self.has_transport() && resolved.otlp_endpoint.is_none() {
            return Err("OTLP transport settings require otlp_endpoint".into());
        }
        let mut config = resolved
            .otlp_endpoint
            .as_ref()
            .map(ExporterConfig::for_endpoint);
        if let Some(config) = config.as_mut() {
            config.batch_records = resolved.otlp_batch_size;
            config.headers = self.otlp_headers.clone();
            config.compression = match self.otlp_compression.as_deref() {
                None | Some("") => false,
                Some("gzip") => true,
                Some(_) => return Err("invalid otlp_compression".into()),
            };
            if let Some(timeout_ms) = self.otlp_timeout_ms {
                if !(1..60_000).contains(&timeout_ms) {
                    return Err("invalid otlp_timeout_ms".into());
                }
                config.request_timeout = Duration::from_millis(timeout_ms);
            }
            config.ca_certificate = self.otlp_ca_certificate.clone();
            config.client_certificate = self.otlp_client_certificate.clone();
            config.client_key = self.otlp_client_key.clone();
        }
        let validated = config.map(OtlpExporter::new).transpose()?.map(Box::new);
        resolved.otlp_exporter = if resolved.export_enabled == Some(false) {
            None
        } else {
            validated
        };
        Ok(resolved)
    }
}

struct ActiveSettings {
    settings: Settings,
    revision: String,
}

pub struct SettingsManager {
    path: Option<PathBuf>,
    base: DaemonArgs,
    active: Mutex<ActiveSettings>,
    updates: watch::Sender<DaemonArgs>,
}

impl SettingsManager {
    /// Build a probe-only exporter from saved settings, including private transport.
    /// This does not persist settings or enable the running export worker.
    pub fn connection_test_exporter(&self) -> Result<OtlpExporter, &'static str> {
        let mut settings = self.active.lock().unwrap().settings.clone();
        if settings.otlp_endpoint.is_none() {
            return Err("no saved OTLP endpoint");
        }
        settings.export_enabled = Some(true);
        settings
            .apply(&self.base)
            .ok()
            .and_then(|args| args.otlp_exporter.map(|exporter| *exporter))
            .ok_or("saved OTLP configuration is invalid")
    }

    pub fn new(
        path: Option<PathBuf>,
        base: DaemonArgs,
        current: DaemonArgs,
        updates: watch::Sender<DaemonArgs>,
    ) -> Self {
        let mut initial = Settings::from_args(&current);
        if let Some(config_path) = base.config.as_ref() {
            if let Ok(bytes) = std::fs::read(config_path) {
                if let Ok(stored) = serde_json::from_slice::<Settings>(&bytes) {
                    initial.copy_transport_from(&stored);
                }
            }
        }
        Self {
            path,
            base,
            active: Mutex::new(ActiveSettings {
                settings: initial,
                revision: Uuid::new_v4().to_string(),
            }),
            updates,
        }
    }

    pub fn snapshot(&self) -> serde_json::Value {
        let active = self.active.lock().unwrap();
        serde_json::json!({ "settings": active.settings.public_json(), "revision": active.revision, "writable": self.path.is_some() })
    }

    pub fn update(
        &self,
        revision: &str,
        mut settings: Settings,
    ) -> Result<serde_json::Value, String> {
        let path = self
            .path
            .as_ref()
            .ok_or("settings are read-only for this daemon")?;
        let mut active = self.active.lock().unwrap();
        if active.revision != revision {
            return Err("settings changed; reload before applying".into());
        }
        if settings.export_enabled.is_none()
            || settings.interval_seconds.is_none()
            || settings.otlp_batch_size.is_none()
        {
            return Err("settings update must include all editable fields".into());
        }
        if settings.has_transport() {
            return Err("transport settings cannot be changed through this interface".into());
        }
        settings.copy_transport_from(&active.settings);
        let resolved = settings
            .apply(&self.base)
            .map_err(|_| "invalid settings or OTLP transport configuration".to_string())?;
        persist(path, &settings).map_err(|error| format!("could not save settings: {error}"))?;
        active.settings = settings;
        active.revision = Uuid::new_v4().to_string();
        self.updates.send_replace(resolved);
        Ok(
            serde_json::json!({ "settings": active.settings.public_json(), "revision": active.revision, "writable": true }),
        )
    }
}

fn persist(path: &Path, settings: &Settings) -> Result<(), Box<dyn std::error::Error>> {
    use std::io::Write;
    let parent = path.parent().ok_or("settings path has no parent")?;
    let parent_existed = parent.exists();
    std::fs::create_dir_all(parent)?;
    #[cfg(unix)]
    if !parent_existed {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700))?;
    }
    let temporary = parent.join(format!(".daemon-{}.json", Uuid::new_v4()));
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(&temporary)?;
    file.write_all(&serde_json::to_vec_pretty(settings)?)?;
    file.sync_all()?;
    std::fs::rename(&temporary, path)?;
    Ok(())
}

pub fn resolve(args: &DaemonArgs) -> Result<DaemonArgs, Box<dyn std::error::Error>> {
    let mut resolved = args.clone();
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
                resolved = settings.apply(&resolved)?;
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
    }
    if resolved.export_enabled.is_none() {
        resolved = Settings::from_args(&resolved).apply(&resolved)?;
    }
    Ok(resolved)
}
