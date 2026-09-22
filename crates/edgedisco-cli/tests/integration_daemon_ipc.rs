#![cfg(unix)]

use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};
use tempfile::TempDir;

struct ChildGuard(Child);

impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn wait_for_socket(path: &Path, child: &mut Child) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline {
        if path.exists() {
            return;
        }
        if let Some(status) = child.try_wait().expect("poll daemon") {
            panic!("daemon exited before socket creation: {status}");
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    panic!("timed out waiting for {}", path.display());
}

fn ipc_request(path: &Path, method: &str) -> Value {
    let mut stream = UnixStream::connect(path).expect("connect to daemon IPC");
    stream
        .set_read_timeout(Some(Duration::from_secs(150)))
        .expect("set timeout");
    writeln!(
        stream,
        "{}",
        json!({"protocol_version":1,"request_id":method,"method":method})
    )
    .expect("write request");
    let mut line = String::new();
    BufReader::new(stream)
        .read_line(&mut line)
        .unwrap_or_else(|error| panic!("read {method} response: {error}"));
    serde_json::from_str(&line).expect("parse response")
}

#[test]
fn compiled_daemon_serves_ipc_scans_and_cleans_up_on_sigint() {
    let temp = TempDir::new().expect("temporary fixture");
    let socket = temp.path().join("state").join("edgedisco.sock");
    let database = temp.path().join("inventory.db");
    let binary = env!("CARGO_BIN_EXE_edgedisco");
    let child = Command::new(binary)
        .args([
            "daemon",
            "--interval",
            "3600",
            "--db",
            database.to_str().unwrap(),
            "--ipc-socket",
            socket.to_str().unwrap(),
        ])
        .env("HOME", temp.path())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn daemon");
    let mut child = ChildGuard(child);
    wait_for_socket(&socket, &mut child.0);

    let negotiate = ipc_request(&socket, "negotiate");
    assert_eq!(negotiate["ok"], true);
    let scan = ipc_request(&socket, "scan");
    assert_eq!(scan["ok"], true);
    assert_eq!(scan["result"]["accepted"], true);
    let status = ipc_request(&socket, "status");
    assert_eq!(status["ok"], true);
    assert_eq!(status["result"]["healthy"], true);

    #[cfg(target_os = "macos")]
    {
        let output = Command::new("lsof")
            .args([
                "-nP",
                "-a",
                "-p",
                &child.0.id().to_string(),
                "-iTCP",
                "-sTCP:LISTEN",
            ])
            .output()
            .expect("run lsof");
        assert!(
            output.stdout.is_empty(),
            "daemon owns TCP listeners:\n{}",
            String::from_utf8_lossy(&output.stdout)
        );
    }

    unsafe {
        libc::kill(child.0.id() as i32, libc::SIGINT);
    }
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Some(status) = child.0.try_wait().expect("wait for daemon") {
            assert!(status.success(), "daemon shutdown status: {status}");
            break;
        }
        assert!(Instant::now() < deadline, "daemon did not shut down");
        std::thread::sleep(Duration::from_millis(25));
    }
    assert!(!socket.exists(), "daemon left its socket behind");
}
