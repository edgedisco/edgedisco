use clap::Parser;
use edgedisco_cli::cli::{Cli, Commands};
use std::path::PathBuf;

#[test]
fn test_parse_scan_command() {
    let cli = Cli::try_parse_from(["edgedisco", "scan"]).expect("parse scan");
    match cli.command {
        Commands::Scan(args) => {
            assert!(!args.pretty);
            assert!(!args.persist);
            assert!(args.db.is_none());
        }
        _ => panic!("expected Scan command"),
    }
}

#[test]
fn test_parse_scan_with_persistence_flag() {
    let cli =
        Cli::try_parse_from(["edgedisco", "scan", "--persist"]).expect("parse persistent scan");
    match cli.command {
        Commands::Scan(args) => {
            assert!(args.persist);
            assert!(args.db.is_none());
        }
        _ => panic!("expected Scan command"),
    }
}

#[test]
fn test_parse_scan_with_pretty_flag() {
    let cli = Cli::try_parse_from(["edgedisco", "scan", "--pretty"]).expect("parse scan pretty");
    match cli.command {
        Commands::Scan(args) => {
            assert!(args.pretty);
        }
        _ => panic!("expected Scan command"),
    }
}

#[test]
fn test_parse_status_command() {
    let cli = Cli::try_parse_from(["edgedisco", "status", "--db", "/tmp/inventory.db"])
        .expect("parse status");
    match cli.command {
        Commands::Status(args) => {
            assert_eq!(args.db, Some(PathBuf::from("/tmp/inventory.db")));
            assert!(!args.json);
        }
        _ => panic!("expected Status command"),
    }
}

#[test]
fn test_parse_daemon_command() {
    let cli = Cli::try_parse_from([
        "edgedisco",
        "daemon",
        "--interval",
        "30",
        "--db",
        "/tmp/custom.db",
        "--once",
        "--otlp-endpoint",
        "https://telemetry.example/v1/logs",
        "--otlp-batch-size",
        "25",
        "--ipc-socket",
        "/tmp/edgedisco.sock",
        "--ipc-mode",
        "system",
        "--ipc-allowed-uid",
        "501",
        "--ipc-allowed-uid",
        "502",
        "--ipc-owner-uid",
        "0",
        "--ipc-group-gid",
        "80",
    ])
    .expect("parse daemon");
    match cli.command {
        Commands::Daemon(args) => {
            assert_eq!(args.interval, 30);
            assert_eq!(args.db, Some(PathBuf::from("/tmp/custom.db")));
            assert!(args.once);
            assert_eq!(
                args.otlp_endpoint.as_deref(),
                Some("https://telemetry.example/v1/logs")
            );
            assert_eq!(args.otlp_batch_size, 25);
            assert_eq!(args.ipc_socket, Some(PathBuf::from("/tmp/edgedisco.sock")));
            assert_eq!(args.ipc_mode, edgedisco_cli::cli::IpcModeArg::System);
            assert_eq!(args.ipc_allowed_uid, vec![501, 502]);
            assert_eq!(args.ipc_owner_uid, Some(0));
            assert_eq!(args.ipc_group_gid, Some(80));
        }
        _ => panic!("expected Daemon command"),
    }
}

#[test]
fn test_parse_service_start_command() {
    let cli = Cli::try_parse_from(["edgedisco", "start", "--root", "server", "agent"])
        .expect("parse start");
    match cli.command {
        Commands::Start(args) => {
            assert!(args.root);
            assert_eq!(args.services, vec!["server", "agent"]);
        }
        _ => panic!("expected Start command"),
    }
}

#[test]
fn test_parse_service_stop_command() {
    let cli = Cli::try_parse_from(["edgedisco", "stop", "otlp-export"]).expect("parse stop");
    match cli.command {
        Commands::Stop(args) => {
            assert!(!args.root);
            assert_eq!(args.services, vec!["otlp-export"]);
        }
        _ => panic!("expected Stop command"),
    }
}

#[test]
fn test_parse_service_restart_command() {
    let cli = Cli::try_parse_from(["edgedisco", "restart", "--root"]).expect("parse restart");
    match cli.command {
        Commands::Restart(args) => {
            assert!(args.root);
            assert!(args.services.is_empty());
        }
        _ => panic!("expected Restart command"),
    }
}

#[test]
fn test_parse_invalid_subcommand_fails() {
    let result = Cli::try_parse_from(["edgedisco", "invalid-subcommand"]);
    assert!(result.is_err());
}
