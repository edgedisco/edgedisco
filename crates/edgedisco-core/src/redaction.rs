use crate::models::{Asset, Device, ScanReport};
use chrono::{DateTime, SecondsFormat};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::path::Path;
use thiserror::Error;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum RedactionError {
    #[error("invalid observation timestamp: {0}")]
    InvalidTimestamp(String),
    #[error("timestamp exceeds permitted clock skew (5 minutes)")]
    ClockSkewExceeded,
    #[error("invalid device field: {0}")]
    InvalidDevice(String),
    #[error("unsupported report fields: {0}")]
    UnsupportedReportFields(String),
    #[error("invalid report field: {0}")]
    InvalidReport(String),
    #[error("invalid asset field: {0}")]
    InvalidAsset(String),
    #[error("unsupported asset kind: {0}")]
    UnsupportedAssetKind(String),
    #[error("invalid metadata field: {0}")]
    InvalidMetadata(String),
    #[error("invalid SHA-256 hash: {0}")]
    InvalidSha256(String),
}

/// Compute SHA-256 hex string for byte slice.
pub fn sha256_digest(input: impl AsRef<[u8]>) -> String {
    let mut hasher = Sha256::new();
    hasher.update(input.as_ref());
    let result = hasher.finalize();
    let mut hex = String::with_capacity(64);
    for byte in result {
        use std::fmt::Write;
        let _ = write!(hex, "{:02x}", byte);
    }
    hex
}

/// Compute SHA-256 hash of a file or directory path string.
pub fn hash_path(path: &str) -> String {
    sha256_digest(path.as_bytes())
}

/// Verify if a string is a 64-character lowercase hexadecimal SHA-256 digest.
pub fn is_valid_sha256(s: &str) -> bool {
    s.len() == 64
        && s.chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
}

/// Sanitize a string cell to prevent spreadsheet formula injection.
pub fn sanitize_csv_cell(value: &str) -> String {
    if value.starts_with(['\t', '\r', '\n']) || value.trim_start().starts_with(['=', '+', '-', '@'])
    {
        format!("'{value}")
    } else {
        value.to_string()
    }
}

/// Parse and normalize ISO-8601 / RFC3339 timestamp.
pub fn validate_timestamp(value: &str) -> Result<String, RedactionError> {
    if value.is_empty() || value.len() > 64 {
        return Err(RedactionError::InvalidTimestamp(value.to_string()));
    }
    DateTime::parse_from_rfc3339(value)
        .map(|timestamp| timestamp.to_rfc3339_opts(SecondsFormat::AutoSi, true))
        .map_err(|_| RedactionError::InvalidTimestamp(value.to_string()))
}

/// Locate only the module, script, package, or image token of a runtime command.
pub fn identity_token_index(parts: &[&str], runtime: &str) -> Option<usize> {
    let python = runtime.starts_with("python");
    let node = runtime == "node" || runtime == "node.exe";
    let runner = runtime == "npx" || runtime == "npx.exe" || runtime == "uvx";

    if python || node || runner {
        let options_with_values: HashSet<&'static str> = if python {
            ["-W", "-X"].into_iter().collect()
        } else if node {
            [
                "-r",
                "--require",
                "--import",
                "--loader",
                "--experimental-loader",
                "--conditions",
                "-C",
            ]
            .into_iter()
            .collect()
        } else {
            [
                "--cache",
                "--registry",
                "--package",
                "-p",
                "--from",
                "--with",
                "--python",
                "--index-url",
            ]
            .into_iter()
            .collect()
        };

        let boolean_options: HashSet<&'static str> = if python {
            ["-B", "-E", "-I", "-s", "-S", "-u", "-v", "-O", "-OO", "-q"]
                .into_iter()
                .collect()
        } else if node {
            [
                "--no-warnings",
                "--enable-source-maps",
                "--inspect",
                "--inspect-brk",
            ]
            .into_iter()
            .collect()
        } else {
            ["-y", "--yes", "--no-install", "--offline", "--no-cache"]
                .into_iter()
                .collect()
        };

        let mut index = 1;
        while index < parts.len() {
            let part = parts[index];
            if part == "--" {
                return if index + 1 < parts.len() {
                    Some(index + 1)
                } else {
                    None
                };
            }
            if part == "-"
                || (python && part.starts_with("-c"))
                || (node && matches!(part, "-e" | "--eval" | "-p" | "--print"))
            {
                return None;
            }
            if python && part == "-m" {
                return if index + 1 < parts.len() {
                    Some(index + 1)
                } else {
                    None
                };
            }
            if !part.starts_with('-') {
                return Some(index);
            }
            if options_with_values.contains(part) {
                index += 2;
            } else if boolean_options.contains(part)
                || (part.contains('=')
                    && options_with_values.contains(part.split('=').next().unwrap_or("")))
                || (python && (part.starts_with("-W") || part.starts_with("-X")))
            {
                index += 1;
            } else {
                return None;
            }
        }
        return None;
    }

    if runtime == "docker" {
        if let Some(run_pos) = parts.iter().position(|&p| p == "run") {
            let docker_boolean_options: HashSet<&'static str> = [
                "-d",
                "--detach",
                "-i",
                "--interactive",
                "--init",
                "--privileged",
                "--read-only",
                "--rm",
                "-t",
                "--tty",
            ]
            .into_iter()
            .collect();

            let mut skip_value = false;
            for (idx, &part) in parts.iter().enumerate().skip(run_pos + 1) {
                if skip_value {
                    skip_value = false;
                } else if part.starts_with('-') {
                    skip_value = !part.contains('=') && !docker_boolean_options.contains(part);
                } else {
                    return Some(idx);
                }
            }
        }
    }

    None
}

/// Compute safe, privacy-preserving command line fingerprint with identity tokens retained and secrets redacted.
pub fn safe_command_fingerprint(parts: &[&str]) -> String {
    if parts.is_empty() {
        return sha256_digest(b"");
    }

    let exe_path = parts[0];
    let runtime = Path::new(exe_path)
        .file_name()
        .and_then(|f| f.to_str())
        .unwrap_or(exe_path)
        .to_lowercase();

    let identity_idx = identity_token_index(parts, &runtime);

    let mut safe = Vec::with_capacity(parts.len());
    safe.push(runtime);

    for (index, &part) in parts.iter().enumerate().skip(1) {
        if Some(index) == identity_idx {
            safe.push(part.to_lowercase().replace('\\', "/"));
        } else if part.starts_with("--") {
            if let Some(key) = part.split('=').next() {
                if part.contains('=') {
                    safe.push(format!("{key}=[REDACTED]"));
                } else {
                    safe.push(part.to_string());
                }
            } else {
                safe.push(part.to_string());
            }
        } else if part.starts_with('-') {
            safe.push(part.chars().take(2).collect());
        } else {
            safe.push("[ARG]".to_string());
        }
    }

    let joined = safe.join("\0");
    sha256_digest(joined.as_bytes())
}

/// Validate device metadata fields against allowlist and length limits.
pub fn validate_device(device: &Device) -> Result<(), RedactionError> {
    if device.hostname.is_empty() || device.hostname.len() > 255 {
        return Err(RedactionError::InvalidDevice("hostname".into()));
    }
    if device.os.is_empty() || device.os.len() > 64 {
        return Err(RedactionError::InvalidDevice("os".into()));
    }
    if let Some(ref ver) = device.os_version {
        if ver.len() > 255 {
            return Err(RedactionError::InvalidDevice("os_version".into()));
        }
    }
    if let Some(ref m) = device.machine {
        if m.len() > 64 {
            return Err(RedactionError::InvalidDevice("machine".into()));
        }
    }
    if let Some(ref av) = device.agent_version {
        if av.len() > 32 {
            return Err(RedactionError::InvalidDevice("agent_version".into()));
        }
    }
    Ok(())
}

/// Validate asset fields against allowlist, length limits, and format invariants.
pub fn validate_asset(asset: &Asset, schema_version: u32) -> Result<(), RedactionError> {
    if asset.fingerprint.is_empty() || asset.fingerprint.len() > 255 {
        return Err(RedactionError::InvalidAsset("fingerprint".into()));
    }
    if asset.name.is_empty() || asset.name.len() > 255 {
        return Err(RedactionError::InvalidAsset("name".into()));
    }
    if asset.vendor.is_empty() || asset.vendor.len() > 255 {
        return Err(RedactionError::InvalidAsset("vendor".into()));
    }
    if !matches!(
        asset.kind.as_str(),
        "application" | "container_application" | "process" | "agent_runtime" | "mcp_server"
    ) {
        return Err(RedactionError::UnsupportedAssetKind(asset.kind.clone()));
    }

    for (label, hash_opt) in [
        ("fingerprint", Some(&asset.fingerprint)),
        ("path_hash", asset.path_hash.as_ref()),
        ("command_hash", asset.command_hash.as_ref()),
        ("binary_sha256", asset.binary_sha256.as_ref()),
    ] {
        if let Some(hash) = hash_opt {
            if !is_valid_sha256(hash) {
                return Err(RedactionError::InvalidSha256(format!("{label}: {hash}")));
            }
        }
    }

    if let Some(ref ver) = asset.version {
        if ver.is_empty() || ver.len() > 128 {
            return Err(RedactionError::InvalidAsset("version".into()));
        }
    }

    if schema_version >= 2 {
        if asset.binary_sha256.is_some() {
            if let Some(ref status) = asset.binary_fingerprint_status {
                if status != "matched" && status != "unlisted" {
                    return Err(RedactionError::InvalidAsset(
                        "binary_fingerprint_status must be 'matched' or 'unlisted'".into(),
                    ));
                }
            } else {
                return Err(RedactionError::InvalidAsset(
                    "binary_fingerprint_status required when binary_sha256 is present".into(),
                ));
            }
            if let Some(ref lib_ver) = asset.fingerprint_library_version {
                if lib_ver.is_empty() || lib_ver.len() > 64 {
                    return Err(RedactionError::InvalidAsset(
                        "invalid fingerprint_library_version".into(),
                    ));
                }
            }
        } else if asset.binary_fingerprint_status.is_some()
            || asset.fingerprint_library_version.is_some()
        {
            return Err(RedactionError::InvalidAsset(
                "binary fingerprint metadata requires binary_sha256".into(),
            ));
        }
    }

    validate_metadata(&asset.metadata)?;

    Ok(())
}

/// Validate asset metadata fields against strict allowlist.
pub fn validate_metadata(
    metadata: &HashMap<String, serde_json::Value>,
) -> Result<(), RedactionError> {
    let allowed_keys: HashSet<&'static str> = [
        "executable",
        "package",
        "configured_in",
        "transport",
        "host_app",
        "runtime",
        "relationship",
        "evidence_label",
        "discovery_source",
        "instance_count",
        "demo_lab",
        "observed_running",
    ]
    .into_iter()
    .collect();

    for (key, val) in metadata {
        if !allowed_keys.contains(key.as_str()) {
            return Err(RedactionError::InvalidMetadata(format!(
                "unsupported key: {key}"
            )));
        }

        match key.as_str() {
            "executable" | "runtime" | "package" => {
                if let Some(s) = val.as_str() {
                    if s.is_empty() || s.len() > 255 || s.contains('/') || s.contains('\\') {
                        return Err(RedactionError::InvalidMetadata(format!(
                            "{key} must be a basename string up to 255 chars"
                        )));
                    }
                } else {
                    return Err(RedactionError::InvalidMetadata(format!(
                        "{key} must be string"
                    )));
                }
            }
            "configured_in" | "transport" | "host_app" | "relationship" | "evidence_label"
            | "discovery_source" => {
                if let Some(s) = val.as_str() {
                    if s.is_empty() || s.len() > 255 {
                        return Err(RedactionError::InvalidMetadata(format!(
                            "{key} must be non-empty string up to 255 chars"
                        )));
                    }
                } else {
                    return Err(RedactionError::InvalidMetadata(format!(
                        "{key} must be string"
                    )));
                }
            }
            "instance_count" => {
                if let Some(cnt) = val.as_i64() {
                    if !(1..=1_000_000).contains(&cnt) {
                        return Err(RedactionError::InvalidMetadata(
                            "instance_count must be between 1 and 1,000,000".into(),
                        ));
                    }
                } else {
                    return Err(RedactionError::InvalidMetadata(
                        "instance_count must be integer".into(),
                    ));
                }
            }
            "demo_lab" | "observed_running" if !val.is_boolean() => {
                return Err(RedactionError::InvalidMetadata(format!(
                    "{key} must be bool"
                )));
            }
            _ => {}
        }
    }
    Ok(())
}

/// Validate full scan report payload.
pub fn validate_report(report: &ScanReport) -> Result<(), RedactionError> {
    if report.scan_id.is_empty() || report.scan_id.len() > 128 {
        return Err(RedactionError::InvalidReport("scan_id".into()));
    }
    validate_timestamp(&report.observed_at)?;

    if report.privacy.content_captured || report.privacy.secrets_captured {
        return Err(RedactionError::InvalidReport(
            "privacy flags violate data safety contract".into(),
        ));
    }
    if !report.privacy.paths_hashed || !report.privacy.command_lines_hashed {
        return Err(RedactionError::InvalidReport(
            "privacy flags must guarantee hashed paths and commands".into(),
        ));
    }

    if report.assets.len() > 10_000 {
        return Err(RedactionError::InvalidReport(
            "assets count exceeds limit of 10,000".into(),
        ));
    }

    let mut seen_fingerprints = HashSet::new();
    let schema_ver = report.schema_version.unwrap_or(1) as u32;

    for asset in &report.assets {
        if !seen_fingerprints.insert(&asset.fingerprint) {
            return Err(RedactionError::InvalidAsset(format!(
                "duplicate asset fingerprint: {}",
                asset.fingerprint
            )));
        }
        validate_asset(asset, schema_ver)?;
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sha256_digest_and_hash_path() {
        let hash = sha256_digest(b"hello world");
        assert_eq!(
            hash,
            "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        );
        assert!(is_valid_sha256(&hash));
        assert!(!is_valid_sha256("INVALID_HASH"));
        assert!(!is_valid_sha256(
            "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcdeG"
        ));

        let path_h = hash_path("/usr/local/bin/claude");
        assert!(is_valid_sha256(&path_h));
    }

    #[test]
    fn test_sanitize_csv_cell() {
        assert_eq!(sanitize_csv_cell("normal"), "normal");
        assert_eq!(sanitize_csv_cell("=SUM(A1:A10)"), "'=SUM(A1:A10)");
        assert_eq!(sanitize_csv_cell("+12345"), "'+12345");
        assert_eq!(sanitize_csv_cell("-50"), "'-50");
        assert_eq!(sanitize_csv_cell("@mention"), "'@mention");
        assert_eq!(sanitize_csv_cell("\tleading_tab"), "'\tleading_tab");
    }

    #[test]
    fn test_safe_command_fingerprint_python() {
        let cmd = [
            "python3",
            "-m",
            "vllm.entrypoints.openai.api_server",
            "--port",
            "8000",
            "--model",
            "meta-llama/Llama-3-8B",
        ];
        let fp = safe_command_fingerprint(&cmd);
        assert!(is_valid_sha256(&fp));

        // Test with different arg values for unpreserved flags
        let cmd2 = [
            "python3",
            "-m",
            "vllm.entrypoints.openai.api_server",
            "--port",
            "9000",
            "--model",
            "other-model",
        ];
        let fp2 = safe_command_fingerprint(&cmd2);
        assert_eq!(fp, fp2);
    }

    #[test]
    fn test_validate_asset_kinds() {
        let valid_asset = Asset::new(
            "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
            "application",
            "Cursor",
            "Anysphere",
            true,
        );
        assert!(validate_asset(&valid_asset, 1).is_ok());

        let mut invalid_kind = valid_asset.clone();
        invalid_kind.kind = "unsupported_kind".into();
        assert!(validate_asset(&invalid_kind, 1).is_err());
    }

    #[test]
    fn test_validate_metadata_basename() {
        let mut asset = Asset::new(
            "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
            "process",
            "claude",
            "Anthropic",
            true,
        );
        asset
            .metadata
            .insert("executable".into(), serde_json::json!("claude"));
        assert!(validate_asset(&asset, 1).is_ok());

        // Full path in executable metadata must be rejected (must be basename)
        asset
            .metadata
            .insert("executable".into(), serde_json::json!("/usr/bin/claude"));
        assert!(validate_asset(&asset, 1).is_err());
    }
}
