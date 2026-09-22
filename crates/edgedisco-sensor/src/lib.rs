//! EdgeDisco Sensor: Native OS process observation and discovery engine.

pub mod container;
pub mod discovery;
pub mod installed;
pub mod installed_apps;
pub mod process;

pub use container::{scan_available_containers, socket_candidates};
pub use discovery::{classify_process, scan_processes};
pub use installed::scan_installed_clis;
pub use installed_apps::scan_installed_apps;
pub use process::{
    default_scanner, scan_host_processes, ProcessObservation, ProcessScanError, ProcessScanner,
};
