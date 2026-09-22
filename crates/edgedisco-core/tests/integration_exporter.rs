use edgedisco_core::exporter::{encode_otlp_request, retry_delay, ExporterConfig, OtlpExporter};
use edgedisco_core::models::OutboxRecord;
use edgedisco_core::store::Store;
use opentelemetry_proto::tonic::collector::logs::v1::ExportLogsServiceRequest;
use opentelemetry_proto::tonic::common::v1::any_value;
use prost::Message;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::thread;
use std::time::Duration;

fn spawn_http_server(statuses: Vec<u16>) -> (String, thread::JoinHandle<Vec<Vec<u8>>>) {
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
                        .contains("content-type: application/x-protobuf"));
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
                        ExportLogsServiceRequest::decode(body).expect("OTLP protobuf payload");
                        payloads.push(body.to_vec());
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
                "HTTP/1.1 {status} {reason}\r\nContent-Type: application/x-protobuf\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            .expect("write response");
        }
        payloads
    });
    (format!("http://{address}/v1/logs"), handle)
}

fn pending_record() -> OutboxRecord {
    let payload = include_str!("../../../tests/fixtures/golden_otlp/observation_v2.json");
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
    let (endpoint, server) = spawn_http_server(vec![503, 202]);
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
    let request = ExportLogsServiceRequest::decode(payloads[0].as_slice()).expect("decode request");
    let resource = request.resource_logs[0]
        .resource
        .as_ref()
        .expect("resource metadata");
    assert!(resource.attributes.iter().any(|attribute| {
        attribute.key == "service.name"
            && attribute
                .value
                .as_ref()
                .and_then(|value| value.value.as_ref())
                == Some(&any_value::Value::StringValue("edgedisco".into()))
    }));
    let scope_logs = &request.resource_logs[0].scope_logs[0];
    let scope = scope_logs.scope.as_ref().expect("instrumentation scope");
    assert_eq!(scope.name, "edgedisco_core.exporter");
    assert_eq!(scope.version, env!("CARGO_PKG_VERSION"));
    let log = &scope_logs.log_records[0];
    assert_eq!(log.event_name, "edgedisco.asset.observed");
    assert!(log.body.is_none(), "asset event body must remain unset");
    assert!(log.attributes.iter().any(|attribute| {
        attribute.key == "asset.name"
            && attribute
                .value
                .as_ref()
                .and_then(|value| value.value.as_ref())
                == Some(&any_value::Value::StringValue("Claude Code".into()))
    }));
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

#[tokio::test]
async fn invalid_payload_is_failed_instead_of_stranding_its_lease() {
    let store = Store::open_in_memory().expect("store");
    let record = OutboxRecord::new(
        "invalid-1",
        "asset-invalid",
        "{}",
        2,
        "2026-09-22T00:00:00Z",
        "2026-09-22T00:00:00Z",
    );
    store.insert_outbox(&record).expect("insert event");
    let exporter = OtlpExporter::new(ExporterConfig {
        endpoint: "http://127.0.0.1:9/v1/logs".into(),
        batch_records: 10,
        batch_bytes: 1024,
        initial_backoff: Duration::from_secs(1),
        max_backoff: Duration::from_secs(8),
        request_timeout: Duration::from_secs(1),
    })
    .expect("exporter");

    let outcome = exporter
        .export_once_at(&store, "2026-09-22T00:00:00Z")
        .await
        .expect("invalid event is quarantined");
    assert_eq!(outcome.failed, 1);
    let failed = store
        .get_outbox("invalid-1")
        .expect("read failed row")
        .expect("failed row exists");
    assert_eq!(failed.status, "failed");
    assert_eq!(failed.last_error_code.as_deref(), Some("invalid_payload"));
    assert!(failed.lease_id.is_none());
}

fn fixture_record(payload: &str) -> OutboxRecord {
    OutboxRecord::new(
        "fixture-outbox-id",
        "fixture-asset-key",
        payload,
        payload.len() as i64,
        "2026-09-21T12:00:01Z",
        "2026-09-21T12:00:01Z",
    )
}

fn assert_matches_python_record(payload: &str, python_wire: &[u8]) {
    let record = fixture_record(payload);
    let native_wire = encode_otlp_request(&[&record]).expect("encode native OTLP protobuf");
    let native = ExportLogsServiceRequest::decode(native_wire.as_slice()).expect("decode native");
    let python = ExportLogsServiceRequest::decode(python_wire).expect("decode Python fixture");

    assert_eq!(native.resource_logs.len(), 1);
    assert_eq!(python.resource_logs.len(), 1);
    let native_resource = &native.resource_logs[0];
    let python_resource = &python.resource_logs[0];
    assert_eq!(native_resource.scope_logs.len(), 1);
    assert_eq!(python_resource.scope_logs.len(), 1);
    assert_eq!(
        native_resource.scope_logs[0].log_records, python_resource.scope_logs[0].log_records,
        "native record schema and values must match the Python golden fixture"
    );

    let native_resource = native_resource.resource.as_ref().expect("native resource");
    let service_name = native_resource
        .attributes
        .iter()
        .find(|attribute| attribute.key == "service.name")
        .and_then(|attribute| attribute.value.as_ref())
        .and_then(|value| value.value.as_ref());
    assert_eq!(
        service_name,
        Some(&any_value::Value::StringValue("edgedisco".into()))
    );
    assert!(native_resource
        .attributes
        .iter()
        .any(|attribute| attribute.key == "service.version"));
}

#[test]
fn native_observation_matches_python_golden_otlp_record() {
    assert_matches_python_record(
        include_str!("../../../tests/fixtures/golden_otlp/observation_v2.json"),
        include_bytes!("../../../tests/fixtures/golden_otlp/observation_v2.pb"),
    );
}

#[test]
fn native_inventory_matches_python_golden_otlp_record() {
    assert_matches_python_record(
        include_str!("../../../tests/fixtures/golden_otlp/device_inventory_v2.json"),
        include_bytes!("../../../tests/fixtures/golden_otlp/device_inventory_v2.pb"),
    );
}

#[test]
fn native_encoder_rejects_fields_outside_the_python_allowlist() {
    let mut payload: serde_json::Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/golden_otlp/observation_v2.json"
    ))
    .expect("fixture JSON");
    payload["attributes"]["prompt"] = serde_json::json!("must not reach telemetry");
    let payload = serde_json::to_string(&payload).expect("serialize adversarial payload");
    let record = fixture_record(&payload);

    let error = encode_otlp_request(&[&record]).expect_err("unknown attribute must be rejected");
    assert!(error.to_string().contains("unsupported outbox attributes"));
}
