#![cfg(target_os = "macos")]

use edgedisco_core::redaction::validate_asset;
use edgedisco_sensor::jetbrains_plugins::scan_jetbrains_plugins_in;
use tempfile::tempdir;

fn jar_with_manifest(xml: &[u8], deflated: bool) -> Vec<u8> {
    use std::io::Write;
    let name = b"META-INF/plugin.xml";
    let compressed = if deflated {
        let mut encoder =
            flate2::write::DeflateEncoder::new(Vec::new(), flate2::Compression::default());
        encoder.write_all(xml).unwrap();
        encoder.finish().unwrap()
    } else {
        xml.to_vec()
    };
    let method = if deflated { 8u16 } else { 0u16 };
    let crc = crc32fast::hash(xml);
    let mut jar = Vec::new();
    jar.extend(0x0403_4b50u32.to_le_bytes());
    jar.extend(20u16.to_le_bytes());
    jar.extend(0u16.to_le_bytes());
    jar.extend(method.to_le_bytes());
    jar.extend([0; 4]);
    jar.extend(crc.to_le_bytes());
    jar.extend((compressed.len() as u32).to_le_bytes());
    jar.extend((xml.len() as u32).to_le_bytes());
    jar.extend((name.len() as u16).to_le_bytes());
    jar.extend(0u16.to_le_bytes());
    jar.extend(name);
    jar.extend(&compressed);
    let central_start = jar.len() as u32;
    jar.extend(0x0201_4b50u32.to_le_bytes());
    jar.extend(20u16.to_le_bytes());
    jar.extend(20u16.to_le_bytes());
    jar.extend(0u16.to_le_bytes());
    jar.extend(method.to_le_bytes());
    jar.extend([0; 4]);
    jar.extend(crc.to_le_bytes());
    jar.extend((compressed.len() as u32).to_le_bytes());
    jar.extend((xml.len() as u32).to_le_bytes());
    jar.extend((name.len() as u16).to_le_bytes());
    jar.extend([0; 12]);
    jar.extend(0u32.to_le_bytes());
    jar.extend(name);
    let central_size = jar.len() as u32 - central_start;
    jar.extend(0x0605_4b50u32.to_le_bytes());
    jar.extend([0; 4]);
    jar.extend(1u16.to_le_bytes());
    jar.extend(1u16.to_le_bytes());
    jar.extend(central_size.to_le_bytes());
    jar.extend(central_start.to_le_bytes());
    jar.extend(0u16.to_le_bytes());
    jar
}

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

#[test]
fn jar_manifest_is_discovered_but_corrupt_or_linked_jars_are_ignored() {
    use std::os::unix::fs::symlink;
    let temp = tempdir().unwrap();
    let root = temp.path().canonicalize().unwrap().join("JetBrains");
    let plugins = root.join("Idea2026.1/plugins");
    let junie_lib = plugins.join("junie/lib");
    std::fs::create_dir_all(&junie_lib).unwrap();
    let xml = b"<idea-plugin><id>org.jetbrains.junie</id><name>Junie</name><version>3.0</version></idea-plugin>";
    std::fs::write(junie_lib.join("junie.jar"), jar_with_manifest(xml, true)).unwrap();
    let assets = scan_jetbrains_plugins_in(&root);
    assert_eq!(assets.len(), 1);
    assert_eq!(assets[0].version.as_deref(), Some("3.0"));
    assert!(validate_asset(&assets[0], 2).is_ok());
    std::fs::write(junie_lib.join("junie.jar"), b"not a jar").unwrap();
    assert!(scan_jetbrains_plugins_in(&root).is_empty());
    let external = temp.path().canonicalize().unwrap().join("external.jar");
    std::fs::write(&external, jar_with_manifest(xml, false)).unwrap();
    std::fs::remove_file(junie_lib.join("junie.jar")).unwrap();
    symlink(external, junie_lib.join("junie.jar")).unwrap();
    assert!(scan_jetbrains_plugins_in(&root).is_empty());
}
