use clap::Parser;
use edgedisco_cli::{config, Cli, Commands};
use tempfile::tempdir;

#[test]
fn settings_updates_persist_and_reject_stale_or_invalid_revisions() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("config/daemon.json");
    let cli = Cli::parse_from(["edgedisco", "daemon"]);
    let Commands::Daemon(args) = cli.command else {
        panic!()
    };
    let (tx, rx) = tokio::sync::watch::channel(args.clone());
    let manager = config::SettingsManager::new(Some(path.clone()), args.clone(), args, tx);
    let first = manager.snapshot();
    let mut settings = config::Settings::from_args(&rx.borrow());
    settings.interval_seconds = Some(125);
    settings.otlp_endpoint = Some("http://127.0.0.1:4318/v1/logs".into());
    settings.export_enabled = Some(true);
    let updated = manager
        .update(first["revision"].as_str().unwrap(), settings.clone())
        .unwrap();
    assert_eq!(rx.borrow().interval, 125);
    let mut restarted = rx.borrow().clone();
    restarted.config = Some(path.clone());
    assert_eq!(config::resolve(&restarted).unwrap().interval, 125);
    let bytes = std::fs::read(&path).unwrap();
    assert!(manager
        .update(first["revision"].as_str().unwrap(), settings.clone())
        .is_err());
    settings.interval_seconds = Some(0);
    assert!(manager
        .update(updated["revision"].as_str().unwrap(), settings)
        .is_err());
    assert_eq!(std::fs::read(&path).unwrap(), bytes);
    assert_eq!(rx.borrow().interval, 125);
}

#[test]
fn ui_settings_preserve_private_otlp_transport_and_disable_live_export() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempdir().unwrap();
    let path = dir.path().join("daemon.json");
    let secret = "Authorization=Bearer%20SECRET";
    std::fs::write(&path, format!(r#"{{"schema_version":1,"otlp_endpoint":"https://collector.example.org/v1/logs","otlp_headers":"{secret}","otlp_compression":"gzip"}}"#)).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
    let cli = Cli::parse_from(["edgedisco", "daemon", "--config", path.to_str().unwrap()]);
    let Commands::Daemon(args) = cli.command else {
        panic!()
    };
    let current = config::resolve(&args).unwrap();
    let (tx, rx) = tokio::sync::watch::channel(current.clone());
    let manager = config::SettingsManager::new(Some(path.clone()), args, current, tx);
    let snapshot = manager.snapshot();
    assert!(!snapshot.to_string().contains("SECRET"));
    assert!(!snapshot.to_string().contains("otlp_headers"));
    let mut settings = config::Settings::from_args(&rx.borrow());
    settings.export_enabled = Some(false);
    let disabled = manager
        .update(snapshot["revision"].as_str().unwrap(), settings.clone())
        .unwrap();
    assert!(rx.borrow().otlp_exporter.is_none());
    assert!(
        manager.connection_test_exporter().is_ok(),
        "saved endpoint can be probed while export is disabled"
    );
    assert!(std::fs::read_to_string(&path).unwrap().contains(secret));
    settings.export_enabled = Some(true);
    manager
        .update(disabled["revision"].as_str().unwrap(), settings)
        .unwrap();
    assert!(rx.borrow().otlp_exporter.is_some());
    assert!(config::resolve(&rx.borrow())
        .unwrap()
        .otlp_exporter
        .is_some());
}

#[tokio::test]
async fn readiness_retries_bad_response_then_accepts_healthy_status() {
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    let dir = tempdir().unwrap();
    let path = dir.path().join("ready.sock");
    let listener = tokio::net::UnixListener::bind(&path).unwrap();
    let server = tokio::spawn(async move {
        for healthy in [false, true] {
            let (stream, _) = listener.accept().await.unwrap();
            let mut reader = BufReader::new(stream);
            let mut request = String::new();
            reader.read_line(&mut request).await.unwrap();
            assert!(request.contains("readiness"));
            let response = serde_json::json!({"protocol_version":1,"request_id":"readiness","ok":true,"result":{"healthy":healthy}});
            reader
                .get_mut()
                .write_all(format!("{response}\n").as_bytes())
                .await
                .unwrap();
        }
    });
    config::check_ready(&path).await.unwrap();
    server.await.unwrap();
}

#[test]
fn persistent_settings_override_defaults_without_rewriting() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("daemon.json");
    let text = r#"{"schema_version":1,"interval_seconds":125,"otlp_batch_size":42}"#;
    std::fs::write(&path, text).unwrap();
    let cli = Cli::parse_from(["edgedisco", "daemon", "--config", path.to_str().unwrap()]);
    let Commands::Daemon(args) = cli.command else {
        panic!()
    };
    let resolved = config::resolve(&args).unwrap();
    assert_eq!(resolved.interval, 125);
    assert_eq!(resolved.otlp_batch_size, 42);
    assert_eq!(std::fs::read_to_string(&path).unwrap(), text);
}

#[test]
fn private_otlp_transport_settings_validate_before_database_open() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("daemon.json");
    let text = r#"{"schema_version":1,"otlp_endpoint":"https://collector.example.org/v1/logs","otlp_headers":"Authorization=Bearer%20SECRET","otlp_compression":"gzip","otlp_timeout_ms":2500}"#;
    std::fs::write(&path, text).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
    }
    let cli = Cli::parse_from(["edgedisco", "daemon", "--config", path.to_str().unwrap()]);
    let Commands::Daemon(args) = cli.command else {
        panic!()
    };
    let resolved = config::resolve(&args).expect("valid private collector settings");
    assert!(resolved.otlp_exporter.is_some());
    assert!(!format!("{resolved:?}").contains("SECRET"));
    assert_eq!(std::fs::read_to_string(&path).unwrap(), text);
}

#[test]
fn bad_configuration_fails_before_database_creation() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("daemon.json");
    let db = dir.path().join("inventory.db");
    for text in [
        r#"{"schema_version":999}"#,
        r#"{"schema_version":1,"unknown":true}"#,
        r#"{"schema_version":1,"interval_seconds":0}"#,
        r#"{"schema_version":1,"otlp_batch_size":0}"#,
        r#"{"schema_version":1,"otlp_endpoint":"invalid"}"#,
        r#"{"schema_version":1,"otlp_headers":"Authorization=SECRET"}"#,
        r#"{"schema_version":1,"otlp_endpoint":"https://example.org/v1/logs","otlp_compression":"zstd"}"#,
        r#"{"schema_version":1,"otlp_endpoint":"https://example.org/v1/logs","otlp_timeout_ms":0}"#,
        r#"{"schema_version":1,"otlp_endpoint":"https://example.org/v1/logs","otlp_client_certificate":"/missing/SECRET.pem"}"#,
        "broken",
    ] {
        std::fs::write(&path, text).unwrap();
        let result = std::process::Command::new(env!("CARGO_BIN_EXE_edgedisco"))
            .args(["daemon", "--prepare", "--config"])
            .arg(&path)
            .arg("--db")
            .arg(&db)
            .output()
            .unwrap();
        assert!(!result.status.success(), "accepted {text}");
        assert!(!db.exists());
    }
}

#[cfg(unix)]
#[test]
fn otlp_headers_require_private_configuration_file() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempdir().unwrap();
    let path = dir.path().join("daemon.json");
    std::fs::write(&path, r#"{"schema_version":1,"otlp_endpoint":"https://example.org/v1/logs","otlp_headers":"Authorization=SECRET"}"#).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
    let cli = Cli::parse_from(["edgedisco", "daemon", "--config", path.to_str().unwrap()]);
    let Commands::Daemon(args) = cli.command else {
        panic!()
    };
    let error = config::resolve(&args).expect_err("world-readable header configuration must fail");
    assert!(!error.to_string().contains("SECRET"));
}

#[test]
fn preparation_accepts_missing_optional_config_and_does_not_scan() {
    let dir = tempdir().unwrap();
    let db = dir.path().join("inventory.db");
    let result = std::process::Command::new(env!("CARGO_BIN_EXE_edgedisco"))
        .args(["daemon", "--prepare", "--config"])
        .arg(dir.path().join("missing.json"))
        .arg("--db")
        .arg(&db)
        .output()
        .unwrap();
    assert!(result.status.success(), "{:?}", result);
    let conn = rusqlite::Connection::open(db).unwrap();
    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM scans", [], |row| row.get(0))
        .unwrap();
    assert_eq!(count, 0);
}
