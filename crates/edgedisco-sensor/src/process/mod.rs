pub mod darwin;
pub mod fallback;
pub mod linux;

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// An observed operating system process.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProcessObservation {
    pub pid: Option<u32>,
    pub ppid: Option<u32>,
    pub executable: String,
    pub args: Vec<String>,
}

impl ProcessObservation {
    pub fn new(
        pid: Option<u32>,
        ppid: Option<u32>,
        executable: impl Into<String>,
        args: Vec<String>,
    ) -> Self {
        Self {
            pid,
            ppid,
            executable: executable.into(),
            args,
        }
    }
}

/// Errors occurring during process inspection.
#[derive(Debug, Error)]
pub enum ProcessScanError {
    #[error("process enumeration unavailable: {0}")]
    Unavailable(String),
    #[error("process inspection io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("malformed process observation: {0}")]
    Malformed(String),
    #[error("process inspection permission denied: {0}")]
    PermissionDenied(String),
}

/// Abstract interface for platform-specific process scanners.
pub trait ProcessScanner: Send + Sync {
    /// Enumerate all currently accessible running processes.
    fn scan(&self) -> Result<Vec<ProcessObservation>, ProcessScanError>;
}

/// Construct the default process scanner for the current compilation target.
pub fn default_scanner() -> Box<dyn ProcessScanner> {
    #[cfg(target_os = "macos")]
    {
        Box::new(darwin::DarwinProcessScanner::new())
    }
    #[cfg(target_os = "linux")]
    {
        Box::new(linux::LinuxProcessScanner::new())
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        Box::new(fallback::FallbackProcessScanner::new())
    }
}

/// Scan current host processes using the default platform scanner.
pub fn scan_host_processes() -> Result<Vec<ProcessObservation>, ProcessScanError> {
    default_scanner().scan()
}
