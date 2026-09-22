//! Bounded inventory of VS Code-compatible editor extensions on macOS.
#[cfg(target_os = "macos")]
use edgedisco_core::catalog::VSCODE_EXTENSION_SIGNATURES;
use edgedisco_core::models::Asset;
#[cfg(target_os = "macos")]
use edgedisco_core::redaction::{hash_path, sha256_digest};
#[cfg(target_os = "macos")]
use std::path::PathBuf;

pub fn scan_editor_extensions() -> Vec<Asset> {
    #[cfg(target_os = "macos")]
    {
        let Some(home) = std::env::var_os("HOME") else {
            return Vec::new();
        };
        let home = PathBuf::from(home);
        let roots = [
            ("Visual Studio Code", home.join(".vscode/extensions")),
            (
                "Visual Studio Code Insiders",
                home.join(".vscode-insiders/extensions"),
            ),
            ("Cursor", home.join(".cursor/extensions")),
            ("Windsurf", home.join(".windsurf/extensions")),
            ("VSCodium", home.join(".vscode-oss/extensions")),
        ];
        scan_editor_extensions_in(&roots)
    }
    #[cfg(not(target_os = "macos"))]
    {
        Vec::new()
    }
}

/// Scans only direct child folders in explicitly supplied extension roots.
#[cfg(target_os = "macos")]
pub fn scan_editor_extensions_in(roots: &[(&str, PathBuf)]) -> Vec<Asset> {
    use crate::safe_metadata::{open_child, open_dir, read_regular};
    use serde_json::Value;
    use std::collections::HashSet;
    use std::ffi::OsStr;

    const MAX_MANIFEST_BYTES: u64 = 256 * 1024;
    const MAX_ROOT_MANIFEST_BYTES: usize = 16 * 1024 * 1024;
    let mut assets = Vec::new();
    for (editor, root) in roots {
        if !root.is_absolute() || editor.is_empty() || editor.len() > 128 {
            continue;
        }
        let Some(parent) = root.parent() else {
            continue;
        };
        let Ok(parent_dir) = open_dir(parent) else {
            continue;
        };
        let Some(root_dir) = root
            .file_name()
            .and_then(|name| open_child(&parent_dir, name, libc::O_DIRECTORY))
        else {
            continue;
        };
        let obsolete: HashSet<String> =
            read_regular(&root_dir, OsStr::new(".obsolete"), MAX_MANIFEST_BYTES)
                .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
                .and_then(|value| value.as_object().cloned())
                .map(|entries| {
                    entries
                        .into_iter()
                        .filter_map(|(name, value)| {
                            (value == Value::Bool(true)).then_some(name.to_ascii_lowercase())
                        })
                        .collect()
                })
                .unwrap_or_default();

        let Ok(entries) = std::fs::read_dir(root) else {
            continue;
        };
        let mut manifest_bytes_read = 0usize;
        for entry in entries.take(10_000).flatten() {
            let candidate_name = entry.file_name();
            if obsolete.contains(&candidate_name.to_string_lossy().to_ascii_lowercase()) {
                continue;
            }
            let Some(candidate_dir) = open_child(&root_dir, &candidate_name, libc::O_DIRECTORY)
            else {
                continue;
            };
            let Some(bytes) = read_regular(
                &candidate_dir,
                OsStr::new("package.json"),
                MAX_MANIFEST_BYTES,
            ) else {
                continue;
            };
            manifest_bytes_read += bytes.len();
            if manifest_bytes_read > MAX_ROOT_MANIFEST_BYTES {
                break;
            }
            let Ok(manifest) = serde_json::from_slice::<Value>(&bytes) else {
                continue;
            };
            let Some(publisher) = manifest.get("publisher").and_then(Value::as_str) else {
                continue;
            };
            let Some(extension_name) = manifest.get("name").and_then(Value::as_str) else {
                continue;
            };
            if publisher.len() > 128 || extension_name.len() > 128 {
                continue;
            }
            let manifest_id = format!("{publisher}.{extension_name}");
            let Some((name, vendor, package)) =
                VSCODE_EXTENSION_SIGNATURES.iter().find_map(|signature| {
                    signature
                        .needles
                        .iter()
                        .find(|id| id.eq_ignore_ascii_case(&manifest_id))
                        .map(|id| (signature.name, signature.vendor, *id))
                })
            else {
                continue;
            };
            let path = entry.path();
            let path_hash = hash_path(&path.to_string_lossy());
            let mut asset = Asset::new(
                sha256_digest(format!("editor_extension:{name}:{editor}:{path_hash}")),
                "application",
                name,
                vendor,
                false,
            );
            asset.path_hash = Some(path_hash);
            asset.version = manifest
                .get("version")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|version| {
                    !version.is_empty()
                        && version.len() <= 128
                        && !version.chars().any(char::is_control)
                })
                .map(str::to_owned);
            asset.metadata.insert("package".into(), package.into());
            asset
                .metadata
                .insert("configured_in".into(), (*editor).into());
            asset.metadata.insert(
                "discovery_source".into(),
                "Editor extension inventory".into(),
            );
            assets.push(asset);
        }
    }
    assets.sort_by(|a, b| a.fingerprint.cmp(&b.fingerprint));
    assets.dedup_by(|a, b| a.fingerprint == b.fingerprint);
    assets
}
