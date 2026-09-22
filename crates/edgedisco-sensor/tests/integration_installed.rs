use edgedisco_core::redaction::validate_asset;
use edgedisco_sensor::installed::scan_installed_clis_in;
use std::fs;
use std::slice::from_ref;

#[test]
fn idle_catalog_cli_is_detected_without_execution_and_disappears_when_removed() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().canonicalize().unwrap().join("bin with spaces");
    fs::create_dir(&root).unwrap();
    fs::write(
        root.join("opencode"),
        b"#!/bin/sh\nprintf executed > \"$0.executed\"\n",
    )
    .unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(root.join("opencode"), fs::Permissions::from_mode(0o755)).unwrap();
    }
    fs::write(root.join("unrelated-tool"), b"ignore me").unwrap();
    let assets = scan_installed_clis_in(from_ref(&root), from_ref(&root));
    assert_eq!(assets.len(), 1);
    assert!(!root.join("opencode.executed").exists());
    let asset = &assets[0];
    assert_eq!(asset.name, "OpenCode");
    assert_eq!(asset.kind, "application");
    assert!(!asset.running);
    assert_eq!(asset.present, Some(true));
    validate_asset(asset, 2).unwrap();
    assert!(!serde_json::to_string(asset)
        .unwrap()
        .contains("bin with spaces"));
    fs::remove_file(root.join("opencode")).unwrap();
    assert!(scan_installed_clis_in(from_ref(&root), from_ref(&root)).is_empty());
}

#[cfg(unix)]
#[test]
fn package_links_stay_within_allowed_roots_and_are_deduplicated() {
    use std::os::unix::fs::symlink;
    let temp = tempfile::tempdir().unwrap();
    let allowed = temp.path().canonicalize().unwrap().join("allowed");
    let bin = allowed.join("bin");
    fs::create_dir_all(&bin).unwrap();
    fs::write(allowed.join("package-entry"), b"package contents").unwrap();
    symlink(allowed.join("package-entry"), bin.join("opencode")).unwrap();
    let roots = [bin.clone(), bin.clone()];
    assert_eq!(scan_installed_clis_in(&roots, from_ref(&allowed)).len(), 1);
    fs::remove_file(bin.join("opencode")).unwrap();
    let outside = temp.path().join("outside");
    fs::write(&outside, b"private contents").unwrap();
    symlink(outside, bin.join("opencode")).unwrap();
    assert!(scan_installed_clis_in(&roots, &[allowed]).is_empty());
}

#[test]
fn unrelated_droid_and_directories_are_not_cli_installations() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().canonicalize().unwrap();
    fs::write(root.join("droid"), b"unrelated DROID executable").unwrap();
    fs::create_dir(root.join("opencode")).unwrap();
    assert!(scan_installed_clis_in(from_ref(&root), from_ref(&root)).is_empty());
}
