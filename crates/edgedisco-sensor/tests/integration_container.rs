#![cfg(unix)]

use edgedisco_core::redaction::validate_asset;
use edgedisco_sensor::container::{scan_socket, socket_candidates};
use std::fs;
use std::io::{Read, Write};
use std::os::unix::net::UnixListener;
use std::path::PathBuf;
use std::thread;
use tempfile::TempDir;

fn spawn_runtime(
    responses: Vec<(&'static str, &'static str)>,
) -> (TempDir, PathBuf, thread::JoinHandle<Vec<String>>) {
    let dir = TempDir::new().expect("temp dir");
    let socket = dir.path().join("docker.sock");
    let listener = UnixListener::bind(&socket).expect("bind mock socket");
    let handle = thread::spawn(move || {
        let mut paths = Vec::new();
        for (expected_path, body) in responses {
            let (mut stream, _) = listener.accept().expect("accept request");
            let mut request = Vec::new();
            loop {
                let mut chunk = [0_u8; 1024];
                let size = stream.read(&mut chunk).expect("read request");
                if size == 0 {
                    break;
                }
                request.extend_from_slice(&chunk[..size]);
                if request.windows(4).any(|window| window == b"\r\n\r\n") {
                    break;
                }
            }
            let request = String::from_utf8_lossy(&request);
            let path = request
                .lines()
                .next()
                .and_then(|line| line.split_whitespace().nth(1))
                .expect("request path")
                .to_string();
            assert_eq!(path, expected_path);
            paths.push(path);
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .expect("write response");
        }
        paths
    });
    (dir, socket, handle)
}

#[test]
fn scans_container_list_and_top_over_unix_socket() {
    let containers = r#"[{"Id":"raw-container-id","Image":"ollama/ollama:latest","ImageID":"sha256:abc","Labels":{"secret":"ignore"},"Config":{"Env":[{"not":"a string"}]}}]"#;
    let top = r#"{"Titles":["PID","PPID","COMMAND"],"Processes":[["7","1","/usr/local/bin/ollama serve"],["8","1","sleep 10"]]}"#;
    let (_dir, socket, server) = spawn_runtime(vec![
        ("/containers/json", containers),
        ("/containers/raw-container-id/top", top),
    ]);

    let assets = scan_socket(&socket).expect("scan mock runtime");
    let paths = server.join().expect("server thread");

    assert_eq!(paths.len(), 2);
    assert!(assets.iter().any(|asset| asset.name == "Ollama"));
    for asset in &assets {
        validate_asset(asset, 1).expect("container asset satisfies privacy schema");
    }
    let encoded = serde_json::to_string(&assets).expect("serialize assets");
    assert!(!encoded.contains("raw-container-id"));
    assert!(!encoded.contains("secret"));
    assert!(!encoded.contains("serve"));
    assert!(!encoded.contains("sleep 10"));
}

#[test]
fn ignores_config_env_without_parsing_it() {
    let containers =
        r#"[{"Id":"id-2","Image":"busybox","Config":{"Env":{"malformed":"and-secret"}}}]"#;
    let top = r#"{"Titles":["PID","PPID","COMMAND"],"Processes":[]}"#;
    let (_dir, socket, server) = spawn_runtime(vec![
        ("/containers/json", containers),
        ("/containers/id-2/top", top),
    ]);

    let assets = scan_socket(&socket).expect("unknown Config.Env must be ignored");
    server.join().expect("server thread");
    assert!(assets.is_empty());
}

#[test]
fn rejects_untrusted_container_ids_before_building_request_paths() {
    let containers = r#"[{"Id":"../secrets?x=1","Image":"busybox"}]"#;
    let (_dir, socket, server) = spawn_runtime(vec![("/containers/json", containers)]);

    let assets = scan_socket(&socket).expect("invalid identifier is ignored");
    server.join().expect("server thread");
    assert!(assets.is_empty());
}

#[test]
fn candidate_paths_are_local_and_include_supported_runtimes() {
    let home = PathBuf::from("/Users/tester");
    let candidates = socket_candidates(Some(&home), Some(501));

    assert!(candidates.contains(&PathBuf::from("/var/run/docker.sock")));
    assert!(candidates.contains(&PathBuf::from("/var/run/podman/podman.sock")));
    assert!(candidates.contains(&home.join(".docker/run/docker.sock")));
    assert!(candidates.contains(&home.join(".colima/default/docker.sock")));
    assert!(candidates.contains(&home.join(".orbstack/run/docker.sock")));
    assert!(candidates.contains(&PathBuf::from("/run/user/501/podman/podman.sock")));
    assert!(candidates
        .iter()
        .all(|path| !path.to_string_lossy().contains("://")));

    // Ensure the temporary fixture cannot leak into later tests if a failure occurs.
    let _ = fs::metadata(home);
}
