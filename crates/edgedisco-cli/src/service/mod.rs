use std::process::Command;
use thiserror::Error;

pub const SERVER_LABEL: &str = "com.edgedisco.server";
pub const AGENT_LABEL: &str = "com.edgedisco.agent";
pub const EXPORTER_LABEL: &str = "com.edgedisco.otlp-export";
pub const SERVICE_LABELS: [&str; 3] = [SERVER_LABEL, AGENT_LABEL, EXPORTER_LABEL];

#[derive(Debug, Error)]
pub enum ServiceError {
    #[error("Unknown service: {0}; choose from server, agent, otlp-export")]
    UnknownService(String),
    #[error("Privilege required: {0}")]
    PrivilegeRequired(String),
    #[error("Service lifecycle is only supported on macOS (Darwin) and Linux: {0}")]
    UnsupportedPlatform(String),
    #[error("Service command failed: {0}")]
    CommandFailed(String),
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ServiceAction {
    Start,
    Stop,
    Restart,
}

/// Resolve input names/aliases to canonical service labels.
pub fn resolve_service_labels(
    services: &[impl AsRef<str>],
) -> Result<Vec<&'static str>, ServiceError> {
    if services.is_empty() {
        return Ok(SERVICE_LABELS.to_vec());
    }

    let mut resolved = Vec::new();
    for item in services {
        let name = item.as_ref().trim().to_lowercase();
        let label = match name.as_str() {
            "server" | "com.edgedisco.server" => SERVER_LABEL,
            "agent" | "com.edgedisco.agent" => AGENT_LABEL,
            "otlp-export" | "exporter" | "com.edgedisco.otlp-export" => EXPORTER_LABEL,
            _ => return Err(ServiceError::UnknownService(item.as_ref().to_string())),
        };
        if !resolved.contains(&label) {
            resolved.push(label);
        }
    }
    Ok(resolved)
}

/// Abstract executor for system service dispatch.
pub trait ServiceExecutor: Send + Sync {
    fn execute(
        &self,
        action: ServiceAction,
        label: &str,
        is_root: bool,
    ) -> Result<String, ServiceError>;
    fn check_root_privileges(&self) -> Result<(), ServiceError>;
}

/// Native OS implementation of service manager.
pub struct NativeServiceExecutor;

impl ServiceExecutor for NativeServiceExecutor {
    fn check_root_privileges(&self) -> Result<(), ServiceError> {
        #[cfg(unix)]
        {
            let euid = unsafe { libc::geteuid() };
            if euid != 0 {
                return Err(ServiceError::PrivilegeRequired(
                    "privileged service lifecycle requires root privileges (UID 0)".to_string(),
                ));
            }
            Ok(())
        }
        #[cfg(not(unix))]
        {
            Err(ServiceError::UnsupportedPlatform(
                "service lifecycle is only supported on macOS (Darwin) and Linux".to_string(),
            ))
        }
    }

    fn execute(
        &self,
        action: ServiceAction,
        label: &str,
        is_root: bool,
    ) -> Result<String, ServiceError> {
        #[cfg(target_os = "macos")]
        {
            let domain = if is_root {
                "system".to_string()
            } else {
                let uid = unsafe { libc::getuid() };
                format!("gui/{uid}")
            };
            let target = format!("{domain}/{label}");

            match action {
                ServiceAction::Start => {
                    let status = Command::new("launchctl")
                        .args(["print", &target])
                        .output()?;
                    if status.status.success() {
                        if label == SERVER_LABEL {
                            let _ = Command::new("launchctl")
                                .args(["kickstart", "-k", &target])
                                .output()?;
                            Ok("started".to_string())
                        } else {
                            Ok("already running".to_string())
                        }
                    } else {
                        let plist_path = if is_root {
                            format!("/Library/LaunchDaemons/{label}.plist")
                        } else {
                            let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
                            format!("{home}/Library/LaunchAgents/{label}.plist")
                        };
                        let out = Command::new("launchctl")
                            .args(["bootstrap", &domain, &plist_path])
                            .output()?;
                        if !out.status.success() {
                            let err = String::from_utf8_lossy(&out.stderr);
                            return Err(ServiceError::CommandFailed(format!(
                                "could not start {label}: {err}"
                            )));
                        }
                        Ok("started".to_string())
                    }
                }
                ServiceAction::Stop => {
                    let status = Command::new("launchctl")
                        .args(["print", &target])
                        .output()?;
                    if !status.status.success() {
                        Ok("already stopped".to_string())
                    } else {
                        let out = Command::new("launchctl")
                            .args(["bootout", &target])
                            .output()?;
                        if !out.status.success() {
                            let err = String::from_utf8_lossy(&out.stderr);
                            return Err(ServiceError::CommandFailed(format!(
                                "could not stop {label}: {err}"
                            )));
                        }
                        Ok("stopped".to_string())
                    }
                }
                ServiceAction::Restart => {
                    let _ = self.execute(ServiceAction::Stop, label, is_root);
                    self.execute(ServiceAction::Start, label, is_root)?;
                    Ok("restarted".to_string())
                }
            }
        }

        #[cfg(target_os = "linux")]
        {
            let mut base_args: Vec<&str> = Vec::new();
            if !is_root {
                base_args.push("--user");
            }
            let unit = format!("{label}.service");

            match action {
                ServiceAction::Start => {
                    let mut is_active_cmd = Command::new("systemctl");
                    is_active_cmd.args(&base_args).args(["is-active", &unit]);
                    let status = is_active_cmd.output()?;
                    let is_active = String::from_utf8_lossy(&status.stdout).trim() == "active";

                    if is_active {
                        if label == SERVER_LABEL {
                            let mut restart_cmd = Command::new("systemctl");
                            restart_cmd.args(&base_args).args(["restart", &unit]);
                            restart_cmd.output()?;
                            Ok("started".to_string())
                        } else {
                            Ok("already running".to_string())
                        }
                    } else {
                        let mut start_cmd = Command::new("systemctl");
                        start_cmd.args(&base_args).args(["start", &unit]);
                        let out = start_cmd.output()?;
                        if !out.status.success() {
                            let err = String::from_utf8_lossy(&out.stderr);
                            return Err(ServiceError::CommandFailed(format!(
                                "could not start {label}: {err}"
                            )));
                        }
                        Ok("started".to_string())
                    }
                }
                ServiceAction::Stop => {
                    let mut is_active_cmd = Command::new("systemctl");
                    is_active_cmd.args(&base_args).args(["is-active", &unit]);
                    let status = is_active_cmd.output()?;
                    let is_active = String::from_utf8_lossy(&status.stdout).trim() == "active";

                    let mut stop_cmd = Command::new("systemctl");
                    stop_cmd.args(&base_args).args(["stop", &unit]);
                    stop_cmd.output()?;

                    if is_active {
                        Ok("stopped".to_string())
                    } else {
                        Ok("already stopped".to_string())
                    }
                }
                ServiceAction::Restart => {
                    let mut restart_cmd = Command::new("systemctl");
                    restart_cmd.args(&base_args).args(["restart", &unit]);
                    let out = restart_cmd.output()?;
                    if !out.status.success() {
                        let err = String::from_utf8_lossy(&out.stderr);
                        return Err(ServiceError::CommandFailed(format!(
                            "could not restart {label}: {err}"
                        )));
                    }
                    Ok("restarted".to_string())
                }
            }
        }

        #[cfg(not(any(target_os = "macos", target_os = "linux")))]
        {
            let _ = (action, label, is_root);
            Err(ServiceError::UnsupportedPlatform(
                "service lifecycle is only supported on macOS (Darwin) and Linux".to_string(),
            ))
        }
    }
}

/// Service coordinator managing sequenced service lifecycle operations.
pub struct ServiceManager {
    executor: Box<dyn ServiceExecutor>,
}

impl Default for ServiceManager {
    fn default() -> Self {
        Self {
            executor: Box::new(NativeServiceExecutor),
        }
    }
}

impl ServiceManager {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_executor(executor: Box<dyn ServiceExecutor>) -> Self {
        Self { executor }
    }

    /// Start services in forward dependency order: server -> agent -> exporter.
    pub fn start(
        &self,
        services: &[impl AsRef<str>],
        is_root: bool,
    ) -> Result<Vec<(String, String)>, ServiceError> {
        if is_root {
            self.executor.check_root_privileges()?;
        }
        let mut targets = resolve_service_labels(services)?;
        // Sort according to canonical SERVICE_LABELS order
        targets.sort_by_key(|lbl| {
            SERVICE_LABELS
                .iter()
                .position(|&s| s == *lbl)
                .unwrap_or(usize::MAX)
        });

        let mut results = Vec::new();
        for label in targets {
            let status = self
                .executor
                .execute(ServiceAction::Start, label, is_root)?;
            results.push((label.to_string(), status));
        }
        Ok(results)
    }

    /// Stop services in reverse dependency order: exporter -> agent -> server.
    pub fn stop(
        &self,
        services: &[impl AsRef<str>],
        is_root: bool,
    ) -> Result<Vec<(String, String)>, ServiceError> {
        if is_root {
            self.executor.check_root_privileges()?;
        }
        let mut targets = resolve_service_labels(services)?;
        // Sort in reverse canonical SERVICE_LABELS order
        targets.sort_by_key(|lbl| {
            std::cmp::Reverse(SERVICE_LABELS.iter().position(|&s| s == *lbl).unwrap_or(0))
        });

        let mut results = Vec::new();
        for label in targets {
            let status = self.executor.execute(ServiceAction::Stop, label, is_root)?;
            results.push((label.to_string(), status));
        }
        Ok(results)
    }

    /// Restart services with sequenced stop-then-start coordination.
    pub fn restart(
        &self,
        services: &[impl AsRef<str>],
        is_root: bool,
    ) -> Result<Vec<(String, String)>, ServiceError> {
        if is_root {
            self.executor.check_root_privileges()?;
        }
        let targets = resolve_service_labels(services)?;
        self.stop(&targets, is_root)?;
        self.start(&targets, is_root)?;
        Ok(targets
            .into_iter()
            .map(|t| (t.to_string(), "restarted".to_string()))
            .collect())
    }
}
