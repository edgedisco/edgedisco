//! EdgeDisco Sensor: Native OS process observation and discovery engine.

pub mod discovery;
pub mod process;

pub use discovery::{classify_process, scan_processes};
pub use process::{
    default_scanner, scan_host_processes, ProcessObservation, ProcessScanError, ProcessScanner,
};
