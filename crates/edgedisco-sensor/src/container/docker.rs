use edgedisco_core::catalog::{classify_process_name, classify_text};
use edgedisco_core::models::Asset;
use edgedisco_core::redaction::sha256_digest;
use serde::Deserialize;
use std::collections::HashSet;
use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::Duration;
use thiserror::Error;

const MAX_RESPONSE_BYTES: usize = 1_048_576;
const SOCKET_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Debug, Error)]
pub enum ContainerScanError {
    #[error("container runtime I/O error: {0}")]
    Io(#[from] std::io::Error),
    #[error("container runtime returned malformed HTTP: {0}")]
    Http(String),
    #[error("container runtime returned malformed JSON: {0}")]
    Json(#[from] serde_json::Error),
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "PascalCase")]
struct ContainerSummary {
    id: String,
    #[serde(default)]
    image: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "PascalCase")]
struct ContainerTop {
    titles: Vec<String>,
    processes: Vec<Vec<String>>,
}

fn http_get(socket: &Path, path: &str) -> Result<Vec<u8>, ContainerScanError> {
    if !path.starts_with('/') || path.contains(['\r', '\n']) {
        return Err(ContainerScanError::Http(
            "invalid local request path".into(),
        ));
    }
    let mut stream = UnixStream::connect(socket)?;
    stream.set_read_timeout(Some(SOCKET_TIMEOUT))?;
    stream.set_write_timeout(Some(SOCKET_TIMEOUT))?;
    write!(
        stream,
        "GET {path} HTTP/1.1\r\nHost: localhost\r\nAccept: application/json\r\nConnection: close\r\n\r\n"
    )?;
    stream.flush()?;

    let mut response = Vec::new();
    stream
        .take((MAX_RESPONSE_BYTES + 1) as u64)
        .read_to_end(&mut response)?;
    if response.len() > MAX_RESPONSE_BYTES {
        return Err(ContainerScanError::Http(
            "response exceeded size limit".into(),
        ));
    }
    let header_end = response
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .ok_or_else(|| ContainerScanError::Http("missing header terminator".into()))?;
    let header = std::str::from_utf8(&response[..header_end])
        .map_err(|_| ContainerScanError::Http("non-UTF-8 response headers".into()))?;
    let status = header
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .ok_or_else(|| ContainerScanError::Http("missing status code".into()))?;
    if status != "200" {
        return Err(ContainerScanError::Http(format!("HTTP status {status}")));
    }
    Ok(response[(header_end + 4)..].to_vec())
}

fn executable_basename(command: &str) -> Option<&str> {
    let executable = command.split_whitespace().next()?;
    Path::new(executable).file_name()?.to_str()
}

fn asset_for(runtime_hash: &str, name: &str, vendor: &str, evidence: &str) -> Asset {
    let fingerprint = sha256_digest(format!("container:{runtime_hash}:{name}:{evidence}"));
    let mut asset = Asset::new(fingerprint, "container_application", name, vendor, true);
    asset.command_hash = Some(runtime_hash.to_string());
    asset
        .metadata
        .insert("transport".into(), serde_json::json!("container"));
    asset
}

pub fn scan_socket(socket: &Path) -> Result<Vec<Asset>, ContainerScanError> {
    let body = http_get(socket, "/containers/json")?;
    let containers: Vec<ContainerSummary> = serde_json::from_slice(&body)?;
    let mut assets = Vec::new();
    let mut seen = HashSet::new();

    for container in containers {
        if container.id.is_empty()
            || container.id.len() > 128
            || !container
                .id
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
        {
            continue;
        }
        let runtime_hash = sha256_digest(format!("{}:{}", socket.display(), container.id));
        if let Some((name, vendor)) = classify_text(&container.image) {
            if seen.insert((runtime_hash.clone(), name.to_string())) {
                assets.push(asset_for(&runtime_hash, name, vendor, "image-name"));
            }
        }

        let top_path = format!("/containers/{}/top", container.id);
        let Ok(top_body) = http_get(socket, &top_path) else {
            continue;
        };
        let Ok(top) = serde_json::from_slice::<ContainerTop>(&top_body) else {
            continue;
        };
        let command_index = top.titles.iter().position(|title| {
            matches!(
                title.to_ascii_uppercase().as_str(),
                "COMMAND" | "CMD" | "COMM"
            )
        });
        let Some(command_index) = command_index else {
            continue;
        };
        for row in top.processes {
            let Some(command) = row.get(command_index) else {
                continue;
            };
            let Some(executable) = executable_basename(command) else {
                continue;
            };
            if let Some((name, vendor)) = classify_process_name(executable) {
                if seen.insert((runtime_hash.clone(), name.to_string())) {
                    assets.push(asset_for(&runtime_hash, name, vendor, "process-basename"));
                }
            }
        }
    }
    Ok(assets)
}
