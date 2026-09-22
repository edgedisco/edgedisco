use edgedisco_cli::ipc::{
    peer_identity, DaemonIpcState, IpcConfig, IpcLimits, IpcServer, PeerIdentity, PeerPolicy,
    ScanCommand, PROTOCOL_VERSION,
};
use edgedisco_core::models::{Asset, Device};
use edgedisco_core::store::Store;
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;
use tempfile::TempDir;
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::{mpsc, watch};

fn test_store(path: &Path) -> Arc<Store> {
    let store = Arc::new(Store::open(path).expect("open store"));
    store
        .enroll_device(&Device::new(
            "dev-test",
            "host-test",
            "macOS",
            Some("15.0".into()),
            Some("arm64".into()),
            Some("0.1.0".into()),
            "must-not-leak-token-hash",
            "2026-09-22T00:00:00Z",
            "2026-09-22T00:00:00Z",
        ))
        .expect("enroll device");
    let mut asset = Asset::new(
        "must-not-leak-fingerprint",
        "application",
        "Cursor",
        "Anysphere",
        true,
    );
    asset.version = Some("1.2.3".into());
    asset.path_hash = Some("must-not-leak-path-hash".into());
    asset.command_hash = Some("must-not-leak-command-hash".into());
    asset
        .metadata
        .insert("secretish".into(), json!("must-not-leak-metadata"));
    store
        .upsert_asset("dev-test", &asset, "2026-09-22T00:00:00Z")
        .expect("persist asset");
    store
}

async fn request(path: &Path, value: Value) -> Value {
    let mut stream = UnixStream::connect(path).await.expect("connect socket");
    let mut encoded = serde_json::to_vec(&value).expect("serialize request");
    encoded.push(b'\n');
    stream.write_all(&encoded).await.expect("write request");
    let mut line = String::new();
    BufReader::new(stream)
        .read_line(&mut line)
        .await
        .expect("read response");
    serde_json::from_str(&line).expect("parse response")
}

#[test]
fn peer_policy_is_explicit_and_rejects_disallowed_identities() {
    let user = PeerPolicy::user(501);
    assert!(user.authorize(PeerIdentity {
        uid: 501,
        gid: 20,
        pid: Some(1)
    }));
    assert!(user.authorize(PeerIdentity {
        uid: 0,
        gid: 0,
        pid: Some(1)
    }));
    assert!(!user.authorize(PeerIdentity {
        uid: 502,
        gid: 20,
        pid: Some(1)
    }));

    let system = PeerPolicy::system(BTreeSet::from([700_u32]));
    assert!(system.authorize(PeerIdentity {
        uid: 700,
        gid: 80,
        pid: Some(2)
    }));
    assert!(system.authorize(PeerIdentity {
        uid: 0,
        gid: 0,
        pid: Some(2)
    }));
    assert!(!system.authorize(PeerIdentity {
        uid: 501,
        gid: 20,
        pid: Some(2)
    }));
}

#[tokio::test]
async fn host_peer_credentials_report_the_connecting_effective_user() {
    let temp = TempDir::new().expect("temp dir");
    let path = temp.path().join("peer.sock");
    let listener = UnixListener::bind(&path).expect("bind");
    let client = UnixStream::connect(&path).await.expect("connect");
    let (server, _) = listener.accept().await.expect("accept");
    let identity = peer_identity(&server).expect("peer credentials");
    assert_eq!(identity.uid, unsafe { libc::geteuid() });
    drop(client);
}

#[tokio::test]
async fn real_socket_negotiates_projects_sanitized_state_and_triggers_scan() {
    let temp = TempDir::new().expect("temp dir");
    let socket = temp.path().join("private").join("edgedisco.sock");
    let store = test_store(&temp.path().join("inventory.db"));
    let state = Arc::new(DaemonIpcState::new("2026-09-22T00:00:00Z"));
    let (scan_tx, mut scan_rx) = mpsc::channel::<ScanCommand>(1);
    let worker_state = Arc::clone(&state);
    let worker = tokio::spawn(async move {
        while let Some(command) = scan_rx.recv().await {
            worker_state.record_scan("2026-09-22T00:01:00Z", 1);
            let _ = command.reply.send(Ok(1));
        }
    });

    let config = IpcConfig::user(socket.clone(), unsafe { libc::geteuid() });
    let server = IpcServer::bind(config, store, state, scan_tx)
        .await
        .expect("bind IPC server");
    let mode = std::fs::metadata(&socket)
        .expect("socket metadata")
        .permissions()
        .mode()
        & 0o777;
    assert_eq!(mode, 0o600);
    let parent_mode = std::fs::metadata(socket.parent().unwrap())
        .expect("parent metadata")
        .permissions()
        .mode()
        & 0o777;
    assert_eq!(parent_mode, 0o700);

    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    let task = tokio::spawn(server.serve(shutdown_rx));
    let slow_client = UnixStream::connect(&socket)
        .await
        .expect("connect slow client without sending a frame");

    let negotiated = request(
        &socket,
        json!({"protocol_version": PROTOCOL_VERSION, "request_id":"n1", "method":"negotiate"}),
    )
    .await;
    assert_eq!(negotiated["ok"], true);
    assert_eq!(negotiated["result"]["protocol_version"], PROTOCOL_VERSION);

    let status = request(
        &socket,
        json!({"protocol_version": PROTOCOL_VERSION, "request_id":"s1", "method":"status"}),
    )
    .await;
    assert_eq!(status["result"]["healthy"], true);
    assert_eq!(status["result"]["device_count"], 1);
    assert_eq!(status["result"]["detection_count"], 1);

    let detections = request(
        &socket,
        json!({"protocol_version": PROTOCOL_VERSION, "request_id":"d1", "method":"detections"}),
    )
    .await;
    assert_eq!(detections["result"]["detections"][0]["name"], "Cursor");
    let serialized = serde_json::to_string(&detections).unwrap();
    for forbidden in [
        "fingerprint",
        "path_hash",
        "command_hash",
        "metadata",
        "token_hash",
        "must-not-leak",
    ] {
        assert!(
            !serialized.contains(forbidden),
            "leaked {forbidden}: {serialized}"
        );
    }

    let scan = request(
        &socket,
        json!({"protocol_version": PROTOCOL_VERSION, "request_id":"x1", "method":"scan"}),
    )
    .await;
    assert_eq!(scan["result"]["accepted"], true);
    assert_eq!(scan["result"]["asset_count"], 1);

    drop(slow_client);
    shutdown_tx.send(true).expect("signal shutdown");
    task.await.expect("join server").expect("serve cleanly");
    assert!(!socket.exists(), "server must remove its own socket");
    drop(worker);
}

#[tokio::test]
async fn stale_non_socket_is_refused_and_owned_stale_socket_is_replaced() {
    let temp = TempDir::new().expect("temp dir");
    let path = temp.path().join("edgedisco.sock");
    std::fs::write(&path, b"do not remove").expect("write blocker");
    let store = test_store(&temp.path().join("inventory.db"));
    let state = Arc::new(DaemonIpcState::new("now"));
    let (scan_tx, _scan_rx) = mpsc::channel(1);
    let result = IpcServer::bind(
        IpcConfig::user(path.clone(), unsafe { libc::geteuid() }),
        Arc::clone(&store),
        Arc::clone(&state),
        scan_tx,
    )
    .await;
    assert!(result.is_err());
    assert_eq!(std::fs::read(&path).unwrap(), b"do not remove");

    std::fs::remove_file(&path).unwrap();
    let active = UnixListener::bind(&path).expect("create active socket");
    let (active_tx, _active_rx) = mpsc::channel(1);
    let active_result = IpcServer::bind(
        IpcConfig::user(path.clone(), unsafe { libc::geteuid() }),
        Arc::clone(&store),
        Arc::clone(&state),
        active_tx,
    )
    .await;
    assert!(active_result.is_err(), "must not replace an active socket");
    drop(active);

    let (scan_tx, _scan_rx) = mpsc::channel(1);
    let server = IpcServer::bind(
        IpcConfig::user(path.clone(), unsafe { libc::geteuid() }),
        store,
        state,
        scan_tx,
    )
    .await
    .expect("replace owned stale socket");
    drop(server);
}

#[tokio::test]
async fn shutdown_drains_bounded_work_and_never_removes_a_replacement_socket() {
    let temp = TempDir::new().expect("temp dir");
    let socket = temp.path().join("edgedisco.sock");
    let store = test_store(&temp.path().join("inventory.db"));
    let state = Arc::new(DaemonIpcState::new("now"));
    let (scan_tx, _scan_rx) = mpsc::channel(1);
    let mut config = IpcConfig::user(socket.clone(), unsafe { libc::geteuid() });
    config.limits.read_timeout = Duration::from_secs(30);
    config.limits.drain_timeout = Duration::from_millis(100);
    let server = IpcServer::bind(config, store, state, scan_tx)
        .await
        .expect("bind");
    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    let task = tokio::spawn(server.serve(shutdown_rx));

    let _in_flight = UnixStream::connect(&socket).await.expect("slow client");
    tokio::time::sleep(Duration::from_millis(20)).await;
    std::fs::remove_file(&socket).expect("unlink owned socket");
    let replacement = UnixListener::bind(&socket).expect("bind replacement socket");

    shutdown_tx.send(true).unwrap();
    tokio::time::timeout(Duration::from_secs(1), task)
        .await
        .expect("bounded shutdown")
        .expect("join")
        .expect("serve");
    assert!(socket.exists(), "cleanup removed a replacement socket");
    drop(replacement);
}

#[tokio::test]
async fn malformed_unknown_oversized_partial_and_slow_clients_fail_closed() {
    let temp = TempDir::new().expect("temp dir");
    let socket = temp.path().join("edgedisco.sock");
    let store = test_store(&temp.path().join("inventory.db"));
    let state = Arc::new(DaemonIpcState::new("now"));
    let (scan_tx, _scan_rx) = mpsc::channel(1);
    let mut config = IpcConfig::user(socket.clone(), unsafe { libc::geteuid() });
    config.limits = IpcLimits {
        max_request_bytes: 128,
        max_response_bytes: 2048,
        max_connections: 1,
        read_timeout: Duration::from_millis(100),
        write_timeout: Duration::from_millis(100),
        scan_timeout: Duration::from_millis(100),
        drain_timeout: Duration::from_secs(1),
    };
    let server = IpcServer::bind(config, store, state, scan_tx)
        .await
        .expect("bind");
    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    let task = tokio::spawn(server.serve(shutdown_rx));

    for payload in [
        b"not-json\n".to_vec(),
        serde_json::to_vec(&json!({"protocol_version":1,"request_id":"u","method":"unknown"}))
            .unwrap()
            .into_iter()
            .chain([b'\n'])
            .collect(),
        vec![b'x'; 129],
    ] {
        let mut stream = UnixStream::connect(&socket).await.unwrap();
        stream.write_all(&payload).await.unwrap();
        if !payload.ends_with(b"\n") {
            stream.shutdown().await.unwrap();
        }
        let mut line = String::new();
        let _ = BufReader::new(stream).read_line(&mut line).await;
    }

    let mut partial = UnixStream::connect(&socket).await.unwrap();
    partial.write_all(b"{\"protocol_version\":1").await.unwrap();
    partial.shutdown().await.unwrap();
    let mut ignored = String::new();
    let _ = BufReader::new(partial).read_line(&mut ignored).await;

    let _slow = UnixStream::connect(&socket).await.unwrap();
    tokio::time::sleep(Duration::from_millis(20)).await;
    let mut overflow = UnixStream::connect(&socket).await.unwrap();
    overflow
        .write_all(b"{\"protocol_version\":1,\"request_id\":\"bounded\",\"method\":\"status\"}\n")
        .await
        .unwrap();
    let mut overflow_response = Vec::new();
    tokio::time::timeout(
        Duration::from_millis(100),
        overflow.read_to_end(&mut overflow_response),
    )
    .await
    .expect("connection over concurrency limit must close")
    .expect("read closed connection");
    assert!(overflow_response.is_empty());
    tokio::time::sleep(Duration::from_millis(130)).await;

    let valid = request(
        &socket,
        json!({"protocol_version":1,"request_id":"ok","method":"status"}),
    )
    .await;
    assert_eq!(valid["ok"], true);

    shutdown_tx.send(true).unwrap();
    task.await.unwrap().unwrap();
}
