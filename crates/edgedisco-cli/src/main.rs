use clap::Parser;
use edgedisco_cli::cli::{Cli, Commands};
use edgedisco_cli::commands::{
    daemon::run_daemon,
    scan::run_scan,
    service::{run_restart, run_start, run_stop},
    status::run_status,
};
use std::process::ExitCode;

#[tokio::main]
async fn main() -> ExitCode {
    let cli = Cli::parse();

    let result = match &cli.command {
        Commands::Scan(args) => run_scan(args),
        Commands::Status(args) => run_status(args),
        Commands::Start(args) => run_start(args),
        Commands::Stop(args) => run_stop(args),
        Commands::Restart(args) => run_restart(args),
        Commands::Daemon(args) => run_daemon(args).await,
    };

    if let Err(e) = result {
        eprintln!("Error: {e}");
        ExitCode::FAILURE
    } else {
        ExitCode::SUCCESS
    }
}
