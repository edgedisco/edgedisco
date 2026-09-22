use clap::{Args, Parser, Subcommand, ValueEnum};
use std::path::PathBuf;

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
pub enum IpcModeArg {
    User,
    System,
}

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

    /// Persist discovered assets to this SQLite inventory database
    #[arg(long)]
    pub db: Option<PathBuf>,

    /// Persist to the default per-user inventory database
    #[arg(long, conflicts_with = "db")]
    pub persist: bool,
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

    /// Target service name(s): daemon, agent, otlp-export, exporter
    #[arg(value_name = "SERVICE")]
    pub services: Vec<String>,
}

#[derive(Debug, Args, Clone)]
pub struct DaemonArgs {
    /// Optional versioned JSON settings file (missing file uses CLI defaults)
    #[arg(long)]
    pub config: Option<PathBuf>,

    /// Validate settings and prepare the database, then exit without collecting
    #[arg(long)]
    pub prepare: bool,

    /// Wait up to 30 seconds for the selected daemon IPC endpoint to answer status
    #[arg(long, conflicts_with_all = ["prepare", "once"])]
    pub check_ready: bool,

    /// Periodic collection interval in seconds (default: 60)
    #[arg(long, default_value_t = 60)]
    pub interval: u64,

    /// Path to SQLite inventory database
    #[arg(long)]
    pub db: Option<PathBuf>,

    /// Run a single collection iteration and exit
    #[arg(long)]
    pub once: bool,

    /// OTLP/HTTP logs endpoint (HTTPS required outside loopback tests)
    #[arg(long)]
    pub otlp_endpoint: Option<String>,

    /// Maximum outbox records per OTLP request
    #[arg(long, default_value_t = 100)]
    pub otlp_batch_size: usize,

    /// Unix-domain socket path for local IPC (defaults by IPC mode)
    #[arg(long)]
    pub ipc_socket: Option<PathBuf>,

    /// Permission and peer-authorization policy for the local IPC socket
    #[arg(long, value_enum, default_value_t = IpcModeArg::User)]
    pub ipc_mode: IpcModeArg,

    /// Numeric peer UID allowed in system IPC mode (repeatable)
    #[arg(long)]
    pub ipc_allowed_uid: Vec<u32>,

    /// Numeric peer GID allowed in system IPC mode (repeatable)
    #[arg(long)]
    pub ipc_allowed_gid: Vec<u32>,

    /// Numeric owner UID applied to a system-mode socket by installation configuration
    #[arg(long, requires = "ipc_group_gid")]
    pub ipc_owner_uid: Option<u32>,

    /// Numeric group GID applied to a system-mode socket by installation configuration
    #[arg(long, requires = "ipc_owner_uid")]
    pub ipc_group_gid: Option<u32>,
}
