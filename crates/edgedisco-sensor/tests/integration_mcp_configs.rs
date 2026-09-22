#![cfg(target_os = "macos")]

use edgedisco_core::redaction::validate_asset;
use edgedisco_sensor::mcp_configs::scan_mcp_configs_in;
use std::path::PathBuf;
use tempfile::tempdir;

#[test]
fn discovers_declared_servers_without_exporting_secrets() {
    let temp = tempdir().unwrap();
    let root = temp.path().canonicalize().unwrap();
    let cursor = root.join(".cursor/mcp.json");
    let vscode = root.join(".vscode/mcp.json");
    std::fs::create_dir_all(cursor.parent().unwrap()).unwrap();
    std::fs::create_dir_all(vscode.parent().unwrap()).unwrap();
    std::fs::write(&cursor, r#"{"mcpServers":{"local":{"command":"/usr/local/bin/npx","args":["secret-arg"],"env":{"TOKEN":"secret-token"}},"remote":{"url":"https://secret.example/api?key=secret-url"}}}"#).unwrap();
    std::fs::write(&vscode, r#"{"servers":{"vscode-tool":{"command":"python3"}},"mcpServers":{"wrong-key":{"command":"bad"}}}"#).unwrap();
    let assets = scan_mcp_configs_in(&[("Cursor", cursor), ("VS Code", vscode)]);
    assert_eq!(assets.len(), 3);
    assert!(assets.iter().all(|asset| validate_asset(asset, 2).is_ok()));
    let local = assets.iter().find(|a| a.name == "local").unwrap();
    assert_eq!(local.metadata["executable"], "npx");
    assert_eq!(local.metadata["transport"], "stdio");
    assert_eq!(
        assets.iter().find(|a| a.name == "remote").unwrap().metadata["transport"],
        "remote"
    );
    let serialized = serde_json::to_string(&assets).unwrap();
    for secret in [
        "secret-arg",
        "secret-token",
        "secret-url",
        &temp.path().to_string_lossy(),
    ] {
        assert!(!serialized.contains(secret));
    }
}

#[test]
fn rejects_symlink_components_and_oversized_or_malformed_configs() {
    use std::os::unix::fs::symlink;
    let temp = tempdir().unwrap();
    let root = temp.path().canonicalize().unwrap();
    let real = root.join("real");
    std::fs::create_dir(&real).unwrap();
    let config = real.join("mcp.json");
    std::fs::write(&config, r#"{"mcpServers":{"hidden":{}}}"#).unwrap();
    let link_dir = root.join("linked");
    symlink(&real, &link_dir).unwrap();
    let link_file = root.join("file.json");
    symlink(&config, &link_file).unwrap();
    let huge = root.join("huge.json");
    std::fs::write(&huge, vec![b'x'; 256 * 1024 + 1]).unwrap();
    let malformed = root.join("bad.json");
    std::fs::write(&malformed, b"not json").unwrap();
    let candidates: Vec<(&str, PathBuf)> = vec![
        ("Cursor", link_dir.join("mcp.json")),
        ("Cursor", link_file),
        ("Cursor", huge),
        ("Cursor", malformed),
    ];
    assert!(scan_mcp_configs_in(&candidates).is_empty());
}
