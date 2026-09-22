//! Bounded macOS application-bundle inventory. No bundle code is executed.
use edgedisco_core::catalog::{catalog, APPLICATION_SIGNATURES};
use edgedisco_core::models::Asset;
use edgedisco_core::redaction::{hash_path, sha256_digest};
use std::path::{Path, PathBuf};

pub fn scan_installed_apps() -> Vec<Asset> {
    #[cfg(target_os = "macos")]
    {
        let mut roots = vec![PathBuf::from("/Applications")];
        if let Some(home) = std::env::var_os("HOME") {
            roots.push(PathBuf::from(home).join("Applications"));
        }
        scan_installed_apps_in(&roots)
    }
    #[cfg(not(target_os = "macos"))]
    {
        Vec::new()
    }
}

/// Direct children of explicit roots only. Symlinked roots and bundles are excluded.
pub fn scan_installed_apps_in(roots: &[PathBuf]) -> Vec<Asset> {
    let mut assets = Vec::new();
    for root in roots {
        if !root.is_absolute() || !is_real_directory(root) {
            continue;
        }
        let Ok(entries) = std::fs::read_dir(root) else {
            continue;
        };
        // A pathological directory cannot make a collection pass unbounded.
        for entry in entries.take(10_000).flatten() {
            let path = entry.path();
            if path.extension().is_none_or(|ext| ext != "app") || !is_real_directory(&path) {
                continue;
            }
            let Some(stem) = path.file_stem().and_then(|stem| stem.to_str()) else {
                continue;
            };
            let Some((name, vendor)) = classify_bundle_name(stem) else {
                continue;
            };
            let path_hash = hash_path(&path.to_string_lossy());
            let mut asset = Asset::new(
                sha256_digest(format!("application:{name}:{path_hash}")),
                "application",
                name,
                vendor,
                false,
            );
            asset.path_hash = Some(path_hash);
            asset.metadata.insert(
                "package".into(),
                path.file_name()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .into_owned()
                    .into(),
            );
            asset
                .metadata
                .insert("discovery_source".into(), "Application bundle name".into());
            assets.push(asset);
        }
    }
    assets.sort_by(|a, b| a.fingerprint.cmp(&b.fingerprint));
    assets.dedup_by(|a, b| a.fingerprint == b.fingerprint);
    assets
}

fn is_real_directory(path: &Path) -> bool {
    std::fs::symlink_metadata(path).is_ok_and(|metadata| metadata.file_type().is_dir())
}

fn classify_bundle_name(stem: &str) -> Option<(&'static str, &'static str)> {
    for agent in &catalog().agents {
        if agent.name.eq_ignore_ascii_case(stem)
            || agent
                .display_markers
                .iter()
                .any(|marker| marker.eq_ignore_ascii_case(stem))
        {
            return Some((agent.name.as_str(), agent.vendor.as_str()));
        }
    }
    for app in APPLICATION_SIGNATURES {
        if app.name.eq_ignore_ascii_case(stem)
            || app
                .needles
                .iter()
                .any(|needle| needle.eq_ignore_ascii_case(stem))
        {
            return Some((app.name, app.vendor));
        }
    }
    None
}
