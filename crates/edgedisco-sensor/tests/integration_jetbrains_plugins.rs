#![cfg(target_os = "macos")]

use edgedisco_core::redaction::validate_asset;
use edgedisco_sensor::jetbrains_plugins::scan_jetbrains_plugins_in;
use tempfile::tempdir;

#[test]
fn direct_known_manifests_are_found_and_unrelated_plugins_ignored() {
    let temp = tempdir().unwrap();
    let root = temp.path().canonicalize().unwrap().join("JetBrains");
    let plugins = root.join("Idea2026.1/plugins");
    for name in ["junie", "unrelated"] {
        std::fs::create_dir_all(plugins.join(name).join("META-INF")).unwrap();
    }
    std::fs::write(plugins.join("junie/META-INF/plugin.xml"), b"<idea-plugin><id>org.jetbrains.junie</id><name>Junie</name><version>2.9</version></idea-plugin>").unwrap();
    std::fs::write(
        plugins.join("unrelated/META-INF/plugin.xml"),
        b"<idea-plugin><name>Other</name></idea-plugin>",
    )
    .unwrap();
    let assets = scan_jetbrains_plugins_in(&root);
    assert_eq!(assets.len(), 1);
    assert_eq!(assets[0].name, "Junie");
    assert_eq!(assets[0].version.as_deref(), Some("2.9"));
    assert!(validate_asset(&assets[0], 2).is_ok());
    assert!(!serde_json::to_string(&assets)
        .unwrap()
        .contains(&root.to_string_lossy().to_string()));
}

#[test]
fn symlinks_nonregular_metadata_and_spoofed_identity_are_ignored() {
    use std::os::unix::fs::symlink;
    let temp = tempdir().unwrap();
    let root = temp.path().canonicalize().unwrap().join("JetBrains");
    let plugins = root.join("Idea2026.1/plugins");
    let external = temp.path().canonicalize().unwrap().join("external");
    std::fs::create_dir_all(external.join("META-INF")).unwrap();
    std::fs::write(
        external.join("META-INF/plugin.xml"),
        b"<idea-plugin><id>org.jetbrains.junie</id><name>Junie</name></idea-plugin>",
    )
    .unwrap();
    std::fs::create_dir_all(&plugins).unwrap();
    symlink(&external, plugins.join("junie")).unwrap();
    std::fs::create_dir_all(plugins.join("junie-fake/META-INF")).unwrap();
    std::fs::write(
        plugins.join("junie-fake/META-INF/plugin.xml"),
        b"<idea-plugin><id>other</id><name>Other</name></idea-plugin>",
    )
    .unwrap();
    assert!(scan_jetbrains_plugins_in(&root).is_empty());
}
