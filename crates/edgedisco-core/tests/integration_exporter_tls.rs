#![cfg(unix)]

use edgedisco_core::exporter::{ExporterConfig, OtlpExporter};
use edgedisco_core::models::OutboxRecord;
use edgedisco_core::store::Store;
use std::io::{BufRead, BufReader};
use std::os::unix::fs::PermissionsExt;
use std::process::{Child, Command, Stdio};

struct Server(Child);

impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

const HTTPS_SERVER: &str = r#"
import http.server
import ssl
import sys

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        assert self.path == '/v1/logs'
        assert self.connection.getpeercert()
        assert self.headers['Content-Type'] == 'application/x-protobuf'
        assert self.rfile.read(int(self.headers['Content-Length']))
        self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()

server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(sys.argv[1], sys.argv[2])
context.load_verify_locations(sys.argv[3])
context.verify_mode = ssl.CERT_REQUIRED
server.socket = context.wrap_socket(server.socket, server_side=True)
print(server.server_port, flush=True)
server.handle_request()
"#;

#[tokio::test]
async fn custom_ca_and_client_identity_complete_mutual_tls_export() {
    let directory = tempfile::tempdir().expect("temporary certificates");
    let ca_cert = directory.path().join("ca.pem");
    let ca_key = directory.path().join("ca-key.pem");
    let cert = directory.path().join("cert.pem");
    let key = directory.path().join("key.pem");
    let request = directory.path().join("leaf.csr");
    let config_path = directory.path().join("leaf.cnf");
    std::fs::write(
        &config_path,
        "[ext]\nsubjectAltName=IP:127.0.0.1\nbasicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth,clientAuth\n",
    )
    .expect("certificate configuration");
    let ca = Command::new("openssl")
        .args([
            "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        ])
        .arg("-keyout")
        .arg(&ca_key)
        .arg("-out")
        .arg(&ca_cert)
        .args([
            "-subj",
            "/CN=EdgeDisco Test CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .expect("generate test CA");
    assert!(ca.success(), "generate test CA");
    let leaf = Command::new("openssl")
        .args(["req", "-newkey", "rsa:2048", "-nodes"])
        .arg("-keyout")
        .arg(&key)
        .arg("-out")
        .arg(&request)
        .args(["-subj", "/CN=localhost"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .expect("generate leaf request");
    assert!(leaf.success(), "generate leaf request");
    let signed = Command::new("openssl")
        .args(["x509", "-req", "-days", "1"])
        .arg("-in")
        .arg(&request)
        .arg("-CA")
        .arg(&ca_cert)
        .arg("-CAkey")
        .arg(&ca_key)
        .arg("-CAcreateserial")
        .arg("-out")
        .arg(&cert)
        .arg("-extfile")
        .arg(&config_path)
        .args(["-extensions", "ext"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .expect("sign leaf certificate");
    assert!(signed.success(), "sign leaf certificate");
    std::fs::set_permissions(&key, std::fs::Permissions::from_mode(0o600))
        .expect("private key permissions");

    let mut server = Server(
        Command::new("python3")
            .arg("-u")
            .arg("-c")
            .arg(HTTPS_SERVER)
            .arg(&cert)
            .arg(&key)
            .arg(&ca_cert)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("start HTTPS collector"),
    );
    let mut line = String::new();
    BufReader::new(server.0.stdout.as_mut().expect("server stdout"))
        .read_line(&mut line)
        .expect("collector port");
    let port: u16 = line.trim().parse().expect("collector port number");

    let mut config = ExporterConfig::for_endpoint(format!("https://127.0.0.1:{port}/v1/logs"));
    config.ca_certificate = Some(ca_cert);
    config.client_certificate = Some(cert);
    config.client_key = Some(key);
    let exporter = OtlpExporter::new(config).expect("configured mTLS exporter");
    let payload = include_str!("../../../tests/fixtures/golden_otlp/observation_v2.json");
    let record = OutboxRecord::new(
        "tls-record",
        "tls-asset",
        payload,
        payload.len() as i64,
        "2026-09-22T00:00:00Z",
        "2026-09-22T00:00:00Z",
    );
    let store = Store::open_in_memory().expect("store");
    store.insert_outbox(&record).expect("enqueue record");

    let result = exporter
        .export_once_at(&store, "2026-09-22T00:00:00Z")
        .await
        .expect("export through mTLS");
    let saved = store.get_outbox("tls-record").unwrap().unwrap();
    assert_eq!(result.delivered, 1, "result={result:?}, record={saved:?}");
    assert_eq!(saved.status, "delivered");
    assert!(server.0.wait().expect("collector exit").success());
}
