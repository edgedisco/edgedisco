//! Bounded static CLI inventory. Candidates are inspected, never executed.
use edgedisco_core::catalog::{catalog, known_binary};
use edgedisco_core::models::Asset;
use edgedisco_core::redaction::{hash_path, sha256_digest};
use std::collections::HashSet;
use std::io::Read;
use std::path::{Path, PathBuf};

pub fn scan_installed_clis() -> Vec<Asset> {
    let home = std::env::var_os("HOME").map(PathBuf::from);
    let mut roots = Vec::new();
    if let Some(home) = home {
        for suffix in [
            ".local/bin",
            ".cargo/bin",
            ".bun/bin",
            ".local/share/pnpm",
            ".opencode/bin",
            "bin",
            "go/bin",
        ] {
            roots.push(home.join(suffix));
        }
    }
    roots.extend([
        PathBuf::from("/opt/homebrew/bin"),
        PathBuf::from("/usr/local/bin"),
    ]);
    let mut allowed = roots.clone();
    allowed.extend([PathBuf::from("/opt/homebrew"), PathBuf::from("/usr/local")]);
    scan_installed_clis_in(&roots, &allowed)
}

/// Explicit roots make the filesystem boundary reproducible in tests.
pub fn scan_installed_clis_in(roots: &[PathBuf], allowed: &[PathBuf]) -> Vec<Asset> {
    let allowed: Vec<_> = allowed
        .iter()
        .filter(|p| p.is_absolute())
        .cloned()
        .collect();
    let mut seen = HashSet::new();
    let mut assets = Vec::new();
    for agent in &catalog().agents {
        for executable in &agent.executables {
            if Path::new(executable).components().count() != 1 || executable == ".." {
                continue;
            }
            for root in roots {
                let candidate = root.join(executable);
                let Ok(resolved) = candidate.canonicalize() else {
                    continue;
                };
                if !allowed.iter().any(|root| resolved.starts_with(root)) || !resolved.is_file() {
                    continue;
                }
                if !seen.insert(resolved.clone()) {
                    continue;
                }
                let mut asset = Asset::new(
                    sha256_digest(format!(
                        "agent_cli:{}:{}",
                        agent.name,
                        hash_path(&candidate.to_string_lossy())
                    )),
                    "application",
                    &agent.name,
                    &agent.vendor,
                    false,
                );
                asset.path_hash = Some(hash_path(&candidate.to_string_lossy()));
                asset.present = Some(true);
                // Bound reads even if a file grows during observation.
                const MAX_BYTES: u64 = 256 * 1024 * 1024;
                if let Ok(file) = std::fs::File::open(&resolved) {
                    if file.metadata().is_ok_and(|m| m.len() <= MAX_BYTES) {
                        let mut bytes = Vec::new();
                        if file.take(MAX_BYTES + 1).read_to_end(&mut bytes).is_ok()
                            && bytes.len() as u64 <= MAX_BYTES
                        {
                            let digest = sha256_digest(&bytes);
                            let matched = known_binary(&agent.name, &digest).is_some();
                            asset.binary_sha256 = Some(digest);
                            asset.binary_fingerprint_status =
                                Some(if matched { "matched" } else { "unlisted" }.into());
                            asset.fingerprint_library_version = Some(catalog().updated.clone());
                        }
                    }
                }
                // Avoid confusing Factory's CLI with unrelated DROID executables.
                if agent.name == "Factory Droid"
                    && asset.binary_fingerprint_status.as_deref() != Some("matched")
                {
                    continue;
                }
                asset
                    .metadata
                    .insert("package".into(), executable.clone().into());
                asset
                    .metadata
                    .insert("discovery_source".into(), "Executable search path".into());
                assets.push(asset);
            }
        }
    }
    assets.sort_by(|a, b| a.fingerprint.cmp(&b.fingerprint));
    assets
}
