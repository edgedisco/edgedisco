use clap::{Args, Parser, Subcommand};
use std::path::PathBuf;

#[derive(Debug, Parser)]
#[command(
    name = "edgedisco",
    version,
    about = "EdgeDisco native endpoint discovery and service lifecycle CLI"
)]
pub struct Cli {
    #[command(subcommand)]
    pub command: Commands,
}

#[derive(Debug, Subcommand)]
pub enum Commands {
    /// Run process discovery and output JSON scan report to stdout
    Scan(ScanArgs),

    /// Display local device and detected asset summary from SQLite store
    Status(StatusArgs),

    /// Start EdgeDisco background services
    Start(ServiceArgs),

    /// Stop EdgeDisco background services in reverse dependency order
    Stop(ServiceArgs),

    /// Restart EdgeDisco background services (sequenced stop-then-start)
    Restart(ServiceArgs),

    /// Run continuous periodic collection daemon
    Daemon(DaemonArgs),
}

#[derive(Debug, Args)]
pub struct ScanArgs {
    /// Format output as pretty-printed JSON
    #[arg(long)]
    pub pretty: bool,
}

#[derive(Debug, Args)]
pub struct StatusArgs {
    /// Path to SQLite database
    #[arg(long)]
    pub db: Option<PathBuf>,

    /// Self-service root directory containing data/inventory.db
    #[arg(long)]
    pub root: Option<PathBuf>,

    /// Output status summary as JSON
    #[arg(long)]
    pub json: bool,
}

#[derive(Debug, Args)]
pub struct ServiceArgs {
    /// Manage privileged system-level services (LaunchDaemon on macOS, systemd system on Linux)
    #[arg(long)]
    pub root: bool,

    /// Target service name(s): server, agent, otlp-export, exporter (default: all installed services)
    #[arg(value_name = "SERVICE")]
    pub services: Vec<String>,
}

#[derive(Debug, Args)]
pub struct DaemonArgs {
    /// Periodic collection interval in seconds (default: 60)
    #[arg(long, default_value_t = 60)]
    pub interval: u64,

    /// Path to SQLite inventory database
    #[arg(long)]
    pub db: Option<PathBuf>,

    /// Run a single collection iteration and exit
    #[arg(long)]
    pub once: bool,
}
