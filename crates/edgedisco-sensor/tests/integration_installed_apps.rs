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
