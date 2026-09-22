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
