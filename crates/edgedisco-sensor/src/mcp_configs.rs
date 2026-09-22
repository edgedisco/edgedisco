//! Bounded discovery of declared MCP servers in local macOS application configs.
use edgedisco_core::models::Asset;
#[cfg(target_os = "macos")]
use edgedisco_core::redaction::{hash_path, sha256_digest};
#[cfg(target_os = "macos")]
use std::path::{Path, PathBuf};

pub fn scan_mcp_configs() -> Vec<Asset> {
    #[cfg(target_os = "macos")]
    {
        let Some(home) = std::env::var_os("HOME") else {
            return Vec::new();
        };
        let home = PathBuf::from(home);
        let candidates = [
            (
                "Claude Desktop",
                home.join(".config/Claude/claude_desktop_config.json"),
            ),
            (
                "Claude Desktop",
                home.join("Library/Application Support/Claude/claude_desktop_config.json"),
            ),
            ("Cursor", home.join(".cursor/mcp.json")),
            ("VS Code", home.join(".vscode/mcp.json")),
            (
                "VS Code",
                home.join("Library/Application Support/Code/User/mcp.json"),
            ),
        ];
        scan_mcp_configs_in(&candidates)
    }
    #[cfg(not(target_os = "macos"))]
    {
        Vec::new()
    }
}

#[cfg(target_os = "macos")]
pub fn scan_mcp_configs_in(candidates: &[(&str, PathBuf)]) -> Vec<Asset> {
    use crate::safe_metadata::{open_child, open_dir, read_regular};
    use serde_json::Value;

    const MAX_CONFIG_BYTES: u64 = 256 * 1024;
    const MAX_SERVERS: usize = 1_000;
    let mut assets = Vec::new();
    for (owner, path) in candidates.iter().take(32) {
        if !matches!(*owner, "Claude Desktop" | "Cursor" | "VS Code") || !path.is_absolute() {
            continue;
        }
        // Resolve every directory component with O_NOFOLLOW, not only the final file.
        let Some(parent) = path.parent() else {
            continue;
        };
        let Ok(mut dir) = open_dir(Path::new("/")) else {
            continue;
        };
        let mut valid = true;
        for component in parent.components() {
            use std::path::Component;
            match component {
                Component::RootDir => {}
                Component::Normal(name) => match open_child(&dir, name, libc::O_DIRECTORY) {
                    Some(next) => dir = next,
                    None => {
                        valid = false;
                        break;
                    }
                },
                _ => {
                    valid = false;
                    break;
                }
            }
        }
        if !valid {
            continue;
        }
        let Some(bytes) = path
            .file_name()
            .and_then(|name| read_regular(&dir, name, MAX_CONFIG_BYTES))
        else {
            continue;
        };
        let Ok(config) = serde_json::from_slice::<Value>(&bytes) else {
            continue;
        };
        let key = if *owner == "VS Code" {
            "servers"
        } else {
            "mcpServers"
        };
        let Some(servers) = config.get(key).and_then(Value::as_object) else {
            continue;
        };
        let path_hash = hash_path(&path.to_string_lossy());
        for (name, server) in servers.iter().take(MAX_SERVERS) {
            if name.is_empty() || name.len() > 128 || name.chars().any(char::is_control) {
                continue;
            }
            let command = server
                .get("command")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            let basename = command.rsplit(['/', '\\']).next().unwrap_or("unknown");
            let basename = if basename.is_empty()
                || basename.len() > 255
                || basename.chars().any(char::is_control)
            {
                "unknown"
            } else {
                basename
            };
            let transport = if server
                .get("url")
                .and_then(Value::as_str)
                .is_some_and(|url| !url.is_empty())
            {
                "remote"
            } else {
                "stdio"
            };
            let mut asset = Asset::new(
                sha256_digest(format!("mcp:{owner}:{name}")),
                "mcp_server",
                name,
                "Unknown",
                false,
            );
            asset.path_hash = Some(path_hash.clone());
            asset.command_hash = Some(sha256_digest(basename));
            asset
                .metadata
                .insert("configured_in".into(), (*owner).into());
            asset.metadata.insert("transport".into(), transport.into());
            asset.metadata.insert("executable".into(), basename.into());
            assets.push(asset);
        }
    }
    assets.sort_by(|a, b| a.fingerprint.cmp(&b.fingerprint));
    assets.dedup_by(|a, b| a.fingerprint == b.fingerprint);
    assets
}
