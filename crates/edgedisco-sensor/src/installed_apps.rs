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
            #[cfg(target_os = "macos")]
            {
                let (version, digest) = read_bundle_details(root, &path);
                asset.version = version;
                if let Some(digest) = digest {
                    asset.binary_fingerprint_status = Some(
                        if edgedisco_core::catalog::known_binary(name, &digest).is_some() {
                            "matched"
                        } else {
                            "unlisted"
                        }
                        .into(),
                    );
                    asset.binary_sha256 = Some(digest);
                    asset.fingerprint_library_version = Some(catalog().updated.clone());
                }
            }
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

#[cfg(target_os = "macos")]
fn read_bundle_details(root: &Path, bundle: &Path) -> (Option<String>, Option<String>) {
    use crate::safe_metadata::{open_child, open_dir, read_regular};
    use std::ffi::OsStr;
    use std::io::Write;
    use std::process::{Command, Stdio};

    const MAX_PLIST_BYTES: u64 = 256 * 1024;

    // Open each path component relative to its already-open parent. A symlink
    // replacement at any component cannot redirect the read outside the root.
    let read = || -> Option<(Option<String>, Option<String>)> {
        let root_dir = open_dir(root).ok()?;
        let bundle_dir = open_child(&root_dir, bundle.file_name()?, libc::O_DIRECTORY)?;
        let contents_dir = open_child(&bundle_dir, OsStr::new("Contents"), libc::O_DIRECTORY)?;
        let plist = read_regular(&contents_dir, OsStr::new("Info.plist"), MAX_PLIST_BYTES)?;
        let extract = |key: &str| -> Option<String> {
            let mut child = Command::new("/usr/bin/plutil")
                .args([
                    "-extract", key, "raw", "-expect", "string", "-n", "-o", "-", "-",
                ])
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn()
                .ok()?;
            child.stdin.take()?.write_all(&plist).ok()?;
            let output = child.wait_with_output().ok()?;
            if output.status.success() {
                let value = std::str::from_utf8(&output.stdout).ok()?.trim();
                if !value.is_empty() && !value.chars().any(char::is_control) {
                    return Some(value.to_owned());
                }
            }
            None
        };
        let version = extract("CFBundleShortVersionString")
            .filter(|v| v.len() <= 128)
            .or_else(|| extract("CFBundleVersion").filter(|v| v.len() <= 128));
        let executable = extract("CFBundleExecutable")
            .filter(|v| v.len() <= 255 && !v.contains(['/', '\\']) && v != "." && v != "..");
        let digest = executable.and_then(|name| {
            let macos_dir = open_child(&contents_dir, OsStr::new("MacOS"), libc::O_DIRECTORY)?;
            let file = open_child(&macos_dir, OsStr::new(&name), libc::O_NONBLOCK)?;
            hash_executable(file)
        });
        Some((version, digest))
    };
    read().unwrap_or((None, None))
}

#[cfg(target_os = "macos")]
fn hash_executable(mut file: std::fs::File) -> Option<String> {
    use sha2::{Digest, Sha256};
    use std::io::Read;
    const MAX_BYTES: u64 = 256 * 1024 * 1024;
    let metadata = file.metadata().ok()?;
    if !metadata.is_file() || metadata.len() > MAX_BYTES {
        return None;
    }
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 64 * 1024];
    let mut total = 0u64;
    loop {
        let n = file.read(&mut buffer).ok()?;
        if n == 0 {
            break;
        }
        total += n as u64;
        if total > MAX_BYTES {
            return None;
        }
        hasher.update(&buffer[..n]);
    }
    (total == metadata.len()).then(|| format!("{:x}", hasher.finalize()))
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
