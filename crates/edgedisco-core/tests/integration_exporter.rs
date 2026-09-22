use edgedisco_core::exporter::{retry_delay, ExporterConfig, OtlpExporter};
use edgedisco_core::models::OutboxRecord;
use edgedisco_core::store::Store;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::thread;
use std::time::Duration;

fn spawn_http_server(statuses: Vec<u16>) -> (String, thread::JoinHandle<Vec<serde_json::Value>>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind mock server");
    let address = listener.local_addr().expect("server address");
    let handle = thread::spawn(move || {
        let mut payloads = Vec::new();
        for status in statuses {
            let (mut stream, _) = listener.accept().expect("accept request");
            let mut received = Vec::new();
            loop {
                let mut chunk = [0_u8; 4096];
                let size = stream.read(&mut chunk).expect("read request");
                assert!(size > 0, "request closed before completion");
                received.extend_from_slice(&chunk[..size]);
                if let Some(header_end) = received.windows(4).position(|w| w == b"\r\n\r\n") {
                    let header = String::from_utf8_lossy(&received[..header_end]);
                    assert!(header.starts_with("POST /v1/logs HTTP/1.1"));
                    assert!(header
                        .to_ascii_lowercase()
                        .contains("content-type: application/json"));
                    let content_length = header
                        .lines()
                        .find_map(|line| {
                            line.split_once(':').and_then(|(name, value)| {
                                name.eq_ignore_ascii_case("content-length")
                                    .then(|| value.trim().parse::<usize>().expect("content length"))
                            })
                        })
                        .expect("content-length header");
                    let body_start = header_end + 4;
                    if received.len() >= body_start + content_length {
                        let body = &received[body_start..body_start + content_length];
                        payloads.push(serde_json::from_slice(body).expect("OTLP JSON payload"));
                        break;
                    }
                }
            }
            let reason = if status == 202 {
                "Accepted"
            } else {
                "Server Error"
            };
            write!(
                stream,
                "HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            .expect("write response");
        }
        payloads
    });
    (format!("http://{address}/v1/logs"), handle)
}

fn pending_record() -> OutboxRecord {
    let payload = r#"{"event":"edgedisco.asset.observed","asset":{"name":"Ollama"}}"#;
    OutboxRecord::new(
        "out-1",
        "asset-1",
        payload,
        payload.len() as i64,
        "2026-09-22T00:00:00Z",
        "2026-09-22T00:00:00Z",
    )
}

#[tokio::test]
async fn retries_with_exponential_backoff_then_marks_accepted_batch_delivered() {
    let store = Store::open_in_memory().expect("store");
    store
        .insert_outbox(&pending_record())
        .expect("insert event");
    let (endpoint, server) = spawn_http_server(vec![500, 202]);
    let exporter = OtlpExporter::new(ExporterConfig {
        endpoint,
        batch_records: 10,
        batch_bytes: 1024 * 1024,
        initial_backoff: Duration::from_secs(1),
        max_backoff: Duration::from_secs(8),
        request_timeout: Duration::from_secs(2),
    })
    .expect("exporter");

    let first = exporter
        .export_once_at(&store, "2026-09-22T00:00:00Z")
        .await
        .expect("first export attempt");
    assert_eq!(first.retried, 1);
    let retry = store.get_outbox("out-1").expect("read retry").unwrap();
    assert_eq!(retry.status, "retry");
    assert_eq!(retry.attempt_count, 1);
    assert_eq!(retry.next_attempt_at, "2026-09-22T00:00:01Z");

    let second = exporter
        .export_once_at(&store, "2026-09-22T00:00:01Z")
        .await
        .expect("second export attempt");
    assert_eq!(second.delivered, 1);
    let delivered = store.get_outbox("out-1").expect("read delivered").unwrap();
    assert_eq!(delivered.status, "delivered");
    assert_eq!(delivered.last_http_status, Some(202));

    let payloads = server.join().expect("server thread");
    assert_eq!(payloads.len(), 2);
    assert!(payloads[0].get("resourceLogs").is_some());
}

#[test]
fn backoff_doubles_and_caps() {
    let initial = Duration::from_secs(2);
    let cap = Duration::from_secs(8);
    assert_eq!(retry_delay(initial, cap, 0), Duration::from_secs(2));
    assert_eq!(retry_delay(initial, cap, 1), Duration::from_secs(4));
    assert_eq!(retry_delay(initial, cap, 2), Duration::from_secs(8));
    assert_eq!(retry_delay(initial, cap, 20), Duration::from_secs(8));
}
