#![cfg(target_os = "macos")]

use edgedisco_core::redaction::validate_asset;
use edgedisco_sensor::editor_extensions::scan_editor_extensions_in;
use serde_json::json;
use std::path::Path;
use tempfile::tempdir;

fn manifest(root: &Path, folder: &str, publisher: &str, name: &str, version: &str) {
    let directory = root.join(folder);
    std::fs::create_dir_all(&directory).unwrap();
    std::fs::write(
        directory.join("package.json"),
        serde_json::to_vec(&json!({
            "publisher": publisher, "name": name, "version": version,
        }))
        .unwrap(),
    )
    .unwrap();
}

#[test]
fn exact_manifest_ids_are_found_while_obsolete_unrelated_and_nested_extensions_are_skipped() {
    let temp = tempdir().unwrap();
    let root = temp.path().join(".cursor/extensions");
    std::fs::create_dir_all(&root).unwrap();
    manifest(
        &root,
        "anthropic.claude-code-1.2.3",
        "anthropic",
        "claude-code",
        "1.2.3",
    );
    manifest(
        &root,
        "github.copilot-chat-1.0",
        "github",
        "copilot-chat",
        "1.0",
    );
    manifest(&root, "github.copilot-0.9", "github", "copilot", "0.9");
    manifest(
        &root,
        "github.copilot-helper-1.0",
        "github",
        "copilot-helper",
        "1.0",
    );
    manifest(
        &root.join("nested"),
        "openai.chatgpt-1.0",
        "openai",
        "chatgpt",
        "1.0",
    );
    std::fs::write(root.join(".obsolete"), br#"{"github.copilot-0.9":true}"#).unwrap();

    let assets = scan_editor_extensions_in(&[("Cursor", root)]);
    assert_eq!(assets.len(), 2);
    assert!(assets
        .iter()
        .any(|a| a.name == "Claude Code" && a.version.as_deref() == Some("1.2.3")));
    assert!(assets
        .iter()
        .any(|a| a.name == "GitHub Copilot" && a.metadata["package"] == "github.copilot-chat"));
    assert!(assets
        .iter()
        .all(|a| a.metadata["configured_in"] == "Cursor"));
    assert!(assets.iter().all(|a| validate_asset(a, 2).is_ok()));
    let serialized = serde_json::to_string(&assets).unwrap();
    assert!(!serialized.contains(&temp.path().to_string_lossy().to_string()));
}

#[test]
fn symlinks_and_oversized_manifests_are_not_read_and_long_versions_are_unknown() {
    use std::os::unix::fs::symlink;
    let temp = tempdir().unwrap();
    let root = temp.path().join(".vscode/extensions");
    let private = temp.path().join("private");
    std::fs::create_dir_all(&root).unwrap();
    std::fs::create_dir(&private).unwrap();
    manifest(&private, "openai.chatgpt-1", "openai", "chatgpt", "secret");
    symlink(
        private.join("openai.chatgpt-1"),
        root.join("openai.chatgpt-1"),
    )
    .unwrap();
    let linked_manifest = root.join("anthropic.claude-code-1/package.json");
    std::fs::create_dir_all(linked_manifest.parent().unwrap()).unwrap();
    symlink(
        private.join("openai.chatgpt-1/package.json"),
        &linked_manifest,
    )
    .unwrap();
    let oversized = root.join("github.copilot-1/package.json");
    std::fs::create_dir_all(oversized.parent().unwrap()).unwrap();
    std::fs::write(&oversized, vec![b'x'; 256 * 1024 + 1]).unwrap();
    manifest(
        &root,
        "continue.continue-1",
        "continue",
        "continue",
        &"v".repeat(129),
    );

    let assets = scan_editor_extensions_in(&[("Visual Studio Code", root.clone())]);
    assert_eq!(assets.len(), 1);
    assert_eq!(assets[0].name, "Continue");
    assert_eq!(assets[0].version, None);

    let linked_parent = temp.path().join("linked-vscode");
    symlink(root.parent().unwrap(), &linked_parent).unwrap();
    assert!(scan_editor_extensions_in(&[("VS Code", linked_parent.join("extensions"))]).is_empty());
}
