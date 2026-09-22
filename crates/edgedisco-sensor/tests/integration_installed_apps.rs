#[cfg(target_os = "macos")]
use edgedisco_core::redaction::{sha256_digest, validate_asset};
use edgedisco_sensor::installed_apps::scan_installed_apps_in;
use std::path::Path;
use tempfile::tempdir;

fn bundle(root: &Path, name: &str) {
    std::fs::create_dir(root.join(format!("{name}.app"))).unwrap();
}

#[test]
fn direct_known_bundles_are_found_without_recursing_or_matching_prefixes() {
    let temp = tempdir().unwrap();
    let apps = temp.path().join("Applications");
    std::fs::create_dir(&apps).unwrap();
    for name in [
        "Cursor",
        "Kiro",
        "Kimi Code",
        "Goose",
        "OpenCode",
        "OpenClaw",
    ] {
        bundle(&apps, name);
    }
    bundle(&apps, "Cursor Backup");
    bundle(&apps, "Unrelated");
    let nested = apps.join("Nested");
    std::fs::create_dir(&nested).unwrap();
    bundle(&nested, "Claude");

    let assets = scan_installed_apps_in(std::slice::from_ref(&apps));
    let names = assets
        .iter()
        .map(|asset| asset.name.as_str())
        .collect::<std::collections::HashSet<_>>();
    assert_eq!(
        names,
        std::collections::HashSet::from([
            "Cursor",
            "Kiro IDE",
            "Kimi Code",
            "Goose",
            "OpenCode",
            "OpenClaw",
        ])
    );
    assert!(assets
        .iter()
        .all(|asset| asset.kind == "application" && !asset.running));
    assert!(assets.iter().all(|asset| asset.metadata["package"]
        .as_str()
        .unwrap()
        .ends_with(".app")));
    let serialized = serde_json::to_string(&assets).unwrap();
    assert!(!serialized.contains(&temp.path().to_string_lossy().to_string()));
}

#[cfg(unix)]
#[test]
fn symlinked_root_and_bundle_are_not_followed() {
    use std::os::unix::fs::symlink;
    let temp = tempdir().unwrap();
    let apps = temp.path().join("Applications");
    let private = temp.path().join("Private");
    std::fs::create_dir(&apps).unwrap();
    std::fs::create_dir(&private).unwrap();
    bundle(&private, "Claude");
    symlink(private.join("Claude.app"), apps.join("Claude.app")).unwrap();
    assert!(scan_installed_apps_in(&[apps]).is_empty());
    let linked_root = temp.path().join("Linked Applications");
    symlink(private, &linked_root).unwrap();
    assert!(scan_installed_apps_in(&[linked_root]).is_empty());
}

#[cfg(target_os = "macos")]
fn info_plist(
    root: &Path,
    app: &str,
    short: Option<&str>,
    build: Option<&str>,
) -> std::path::PathBuf {
    let contents = root.join(format!("{app}.app/Contents"));
    std::fs::create_dir_all(&contents).unwrap();
    let mut fields = String::new();
    if let Some(value) = short {
        fields.push_str(&format!(
            "<key>CFBundleShortVersionString</key><string>{value}</string>"
        ));
    }
    if let Some(value) = build {
        fields.push_str(&format!(
            "<key>CFBundleVersion</key><string>{value}</string>"
        ));
    }
    let info = contents.join("Info.plist");
    std::fs::write(&info, format!("<?xml version=\"1.0\" encoding=\"UTF-8\"?><plist version=\"1.0\"><dict>{fields}</dict></plist>")).unwrap();
    info
}

#[cfg(target_os = "macos")]
#[test]
fn reads_short_version_then_falls_back_to_build_version_for_xml_and_binary_plists() {
    let temp = tempdir().unwrap();
    let apps = temp.path().join("Applications");
    std::fs::create_dir(&apps).unwrap();
    info_plist(&apps, "Cursor", Some("1.2.3"), Some("456"));
    info_plist(&apps, "Kiro", None, Some("789"));
    let binary_info = info_plist(&apps, "Goose", Some("2.0"), None);
    assert!(std::process::Command::new("/usr/bin/plutil")
        .args(["-convert", "binary1", "-o"])
        .arg(&binary_info)
        .arg(&binary_info)
        .status()
        .unwrap()
        .success());

    let assets = scan_installed_apps_in(&[apps]);
    let version = |name: &str| {
        assets
            .iter()
            .find(|asset| asset.name == name)
            .unwrap()
            .version
            .as_deref()
    };
    assert_eq!(version("Cursor"), Some("1.2.3"));
    assert_eq!(version("Kiro IDE"), Some("789"));
    assert_eq!(version("Goose"), Some("2.0"));
}

#[cfg(target_os = "macos")]
#[test]
fn rejects_symlinked_and_oversized_bundle_metadata_without_losing_detection() {
    use std::os::unix::fs::symlink;
    let temp = tempdir().unwrap();
    let apps = temp.path().join("Applications");
    let private = temp.path().join("Private");
    std::fs::create_dir(&apps).unwrap();
    std::fs::create_dir(&private).unwrap();
    let private_info = info_plist(&private, "Cursor", Some("secret"), None);

    let linked_contents = apps.join("Cursor.app/Contents");
    std::fs::create_dir(apps.join("Cursor.app")).unwrap();
    symlink(private_info.parent().unwrap(), &linked_contents).unwrap();
    let linked_info = apps.join("Kiro.app/Contents/Info.plist");
    std::fs::create_dir_all(linked_info.parent().unwrap()).unwrap();
    symlink(&private_info, &linked_info).unwrap();
    let large_info = apps.join("Goose.app/Contents/Info.plist");
    std::fs::create_dir_all(large_info.parent().unwrap()).unwrap();
    std::fs::write(&large_info, vec![b'x'; 256 * 1024 + 1]).unwrap();

    let assets = scan_installed_apps_in(&[apps]);
    assert_eq!(assets.len(), 3);
    assert!(assets.iter().all(|asset| asset.version.is_none()));
}

#[cfg(target_os = "macos")]
#[test]
fn bundle_executable_evidence_is_bounded_and_never_follows_links() {
    use std::os::unix::fs::symlink;
    let temp = tempdir().unwrap();
    let apps = temp.path().join("Applications");
    std::fs::create_dir(&apps).unwrap();
    for (app, executable) in [("Cursor", "Cursor"), ("Kiro", "Kiro"), ("Goose", "Goose")] {
        let contents = apps.join(format!("{app}.app/Contents"));
        std::fs::create_dir_all(contents.join("MacOS")).unwrap();
        std::fs::write(contents.join("Info.plist"), format!("<?xml version=\"1.0\"?><plist version=\"1.0\"><dict><key>CFBundleExecutable</key><string>{executable}</string></dict></plist>")).unwrap();
    }
    let cursor = apps.join("Cursor.app/Contents/MacOS/Cursor");
    std::fs::write(&cursor, b"safe-executable").unwrap();
    let private = temp.path().join("private-bin");
    std::fs::write(&private, b"private-executable").unwrap();
    symlink(&private, apps.join("Kiro.app/Contents/MacOS/Kiro")).unwrap();
    let huge = std::fs::File::create(apps.join("Goose.app/Contents/MacOS/Goose")).unwrap();
    huge.set_len(256 * 1024 * 1024 + 1).unwrap();

    let assets = scan_installed_apps_in(&[apps]);
    assert_eq!(assets.len(), 3);
    let cursor_asset = assets.iter().find(|asset| asset.name == "Cursor").unwrap();
    assert_eq!(
        cursor_asset.binary_sha256.as_deref(),
        Some(sha256_digest(b"safe-executable").as_str())
    );
    assert_eq!(
        cursor_asset.binary_fingerprint_status.as_deref(),
        Some("unlisted")
    );
    assert!(assets.iter().all(|asset| validate_asset(asset, 2).is_ok()));
    assert!(assets
        .iter()
        .filter(|asset| asset.name != "Cursor")
        .all(|asset| asset.binary_sha256.is_none()));
}
