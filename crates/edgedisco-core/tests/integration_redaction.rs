use edgedisco_core::models::{Asset, Device, DeviceReport, PrivacyFlags, ScanReport};
use edgedisco_core::redaction::{
    hash_path, is_valid_sha256, safe_command_fingerprint, sanitize_csv_cell, sha256_digest,
    validate_asset, validate_device, validate_report, validate_timestamp, RedactionError,
};

#[test]
fn test_sha256_and_hashing() {
    let digest = sha256_digest("test string");
    assert_eq!(
        digest,
        "d5579c46dfcc7f18207013e65b44e4cb4e2c2298f4ac457ba8f82743f31e930b"
    );
    assert!(is_valid_sha256(&digest));

    let path_h = hash_path("/System/Applications/Utilities/Terminal.app");
    assert!(is_valid_sha256(&path_h));
}

#[test]
fn test_csv_formula_injection_sanitization() {
    assert_eq!(sanitize_csv_cell("clean text"), "clean text");
    assert_eq!(
        sanitize_csv_cell("=CMD|' /C calc'!A0"),
        "'=CMD|' /C calc'!A0"
    );
    assert_eq!(sanitize_csv_cell("+100"), "'+100");
    assert_eq!(sanitize_csv_cell("-200"), "'-200");
    assert_eq!(sanitize_csv_cell("@alert()"), "'@alert()");
    assert_eq!(sanitize_csv_cell("\tleading_tab"), "'\tleading_tab");
    assert_eq!(sanitize_csv_cell("\nleading_newline"), "'\nleading_newline");
    assert_eq!(sanitize_csv_cell("\rleading_cr"), "'\rleading_cr");
}

#[test]
fn test_command_line_redaction_variants() {
    // Docker command
    let docker_cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        "my-ollama",
        "-p",
        "11434:11434",
        "ollama/ollama:latest",
    ];
    let fp_docker = safe_command_fingerprint(&docker_cmd);
    assert!(is_valid_sha256(&fp_docker));

    // Python command
    let py_cmd = ["python3", "-m", "crewai.cli", "run", "--verbose"];
    let fp_py = safe_command_fingerprint(&py_cmd);
    assert!(is_valid_sha256(&fp_py));

    // Node command
    let node_cmd = ["node", "dist/cli.js", "--secret=token123", "-v"];
    let fp_node = safe_command_fingerprint(&node_cmd);
    assert!(is_valid_sha256(&fp_node));
}

#[test]
fn test_validate_device_constraints() {
    let mut dev = Device::new(
        "dev-1",
        "valid-host",
        "Darwin",
        None,
        None,
        None,
        "token",
        "2026-09-21T00:00:00Z",
        "2026-09-21T00:00:00Z",
    );
    assert!(validate_device(&dev).is_ok());

    // Empty hostname
    dev.hostname = "".into();
    assert!(validate_device(&dev).is_err());

    // Hostname over 255 chars
    dev.hostname = "a".repeat(256);
    assert!(validate_device(&dev).is_err());
}

#[test]
fn test_validate_asset_negative_cases() {
    let valid_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    let mut asset = Asset::new(valid_sha, "application", "Claude", "Anthropic", true);

    assert!(validate_asset(&asset, 1).is_ok());

    // Invalid fingerprint (non hex)
    asset.fingerprint = "invalid_hash_string".into();
    assert!(matches!(
        validate_asset(&asset, 1),
        Err(RedactionError::InvalidSha256(_))
    ));

    // Reset fingerprint
    asset.fingerprint = valid_sha.into();

    // Invalid kind
    asset.kind = "malicious_kind".into();
    assert!(matches!(
        validate_asset(&asset, 1),
        Err(RedactionError::UnsupportedAssetKind(_))
    ));

    asset.kind = "application".into();

    // Invalid binary status in v2
    asset.binary_sha256 = Some(valid_sha.into());
    asset.binary_fingerprint_status = Some("invalid_status".into());
    asset.fingerprint_library_version = Some("1.0".into());
    assert!(matches!(
        validate_asset(&asset, 2),
        Err(RedactionError::InvalidAsset(_))
    ));

    // Valid binary status in v2
    asset.binary_fingerprint_status = Some("matched".into());
    assert!(validate_asset(&asset, 2).is_ok());
}

#[test]
fn test_validate_scan_report() {
    let valid_sha = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    let asset = Asset::new(valid_sha, "application", "Windsurf", "Codeium", true);

    let report = ScanReport {
        schema_version: Some(1),
        scan_id: "scan-001".into(),
        observed_at: "2026-09-21T12:00:00Z".into(),
        device: DeviceReport {
            hostname: "mark-laptop".into(),
            os: "Darwin".into(),
            os_version: Some("15.0".into()),
            machine: Some("arm64".into()),
            agent_version: Some("0.1.0".into()),
        },
        assets: vec![asset],
        privacy: PrivacyFlags::default(),
    };

    assert!(validate_report(&report).is_ok());

    // Violating privacy flags must fail
    let mut unsafe_report = report.clone();
    unsafe_report.privacy.content_captured = true;
    assert!(validate_report(&unsafe_report).is_err());

    let mut unhashed_report = report.clone();
    unhashed_report.privacy.paths_hashed = false;
    assert!(validate_report(&unhashed_report).is_err());
}

#[test]
fn test_validate_timestamp_formats() {
    assert!(validate_timestamp("2026-09-21T12:00:00Z").is_ok());
    assert!(validate_timestamp("2026-09-21T12:00:00+00:00").is_ok());
    assert!(validate_timestamp("2026-09-21T12:00:00-07:00").is_ok());

    // Missing timezone
    assert!(validate_timestamp("2026-09-21T12:00:00").is_err());
    assert!(validate_timestamp("").is_err());
    assert!(validate_timestamp("x").is_err());
    assert!(validate_timestamp("not-a-dateZ").is_err());
    assert!(validate_timestamp("é").is_err());
}
