//! Conservative inventory of directly manifested JetBrains plugins on macOS.
use edgedisco_core::models::Asset;
#[cfg(target_os = "macos")]
use edgedisco_core::redaction::{hash_path, sha256_digest};
#[cfg(target_os = "macos")]
use std::path::{Path, PathBuf};

pub fn scan_jetbrains_plugins() -> Vec<Asset> {
    #[cfg(target_os = "macos")]
    {
        let Some(home) = std::env::var_os("HOME") else {
            return Vec::new();
        };
        scan_jetbrains_plugins_in(
            &PathBuf::from(home).join("Library/Application Support/JetBrains"),
        )
    }
    #[cfg(not(target_os = "macos"))]
    {
        Vec::new()
    }
}

#[cfg(target_os = "macos")]
pub fn scan_jetbrains_plugins_in(root: &Path) -> Vec<Asset> {
    use crate::safe_metadata::{open_child, open_dir, read_regular};
    use std::ffi::OsStr;
    use std::path::Component;

    if !root.is_absolute() {
        return Vec::new();
    }
    let Ok(mut root_dir) = open_dir(Path::new("/")) else {
        return Vec::new();
    };
    for component in root.components() {
        match component {
            Component::RootDir => {}
            Component::Normal(name) => match open_child(&root_dir, name, libc::O_DIRECTORY) {
                Some(dir) => root_dir = dir,
                None => return Vec::new(),
            },
            _ => return Vec::new(),
        }
    }
    let Ok(products) = std::fs::read_dir(root) else {
        return Vec::new();
    };
    let mut assets = Vec::new();
    for product in products.take(256).flatten() {
        let Some(product_dir) = open_child(&root_dir, &product.file_name(), libc::O_DIRECTORY)
        else {
            continue;
        };
        let Some(plugins_dir) = open_child(&product_dir, OsStr::new("plugins"), libc::O_DIRECTORY)
        else {
            continue;
        };
        let plugins_path = product.path().join("plugins");
        let Ok(plugins) = std::fs::read_dir(&plugins_path) else {
            continue;
        };
        for plugin in plugins.take(2_000).flatten() {
            let folder = plugin.file_name().to_string_lossy().to_ascii_lowercase();
            let candidate = if folder == "junie" || folder.starts_with("junie-") {
                0
            } else if folder == "continue"
                || folder.starts_with("continue-")
                || folder == "continue-intellij-extension"
            {
                1
            } else if matches!(
                folder.as_str(),
                "kilo code" | "kilo-code" | "kilocode" | "kilo.jetbrains"
            ) {
                2
            } else {
                continue;
            };
            let Some(plugin_dir) = open_child(&plugins_dir, &plugin.file_name(), libc::O_DIRECTORY)
            else {
                continue;
            };
            let direct = open_child(&plugin_dir, OsStr::new("META-INF"), libc::O_DIRECTORY)
                .and_then(|meta_dir| read_regular(&meta_dir, OsStr::new("plugin.xml"), 131_072));
            let Some(bytes) = direct.or_else(|| read_jar_manifest(&plugin_dir, &plugin.path()))
            else {
                continue;
            };
            let Some((id, display, version)) = parse_identity(&bytes) else {
                continue;
            };
            let (name, vendor) = match candidate {
                0 if id.eq_ignore_ascii_case("org.jetbrains.junie")
                    || display.eq_ignore_ascii_case("junie") =>
                {
                    ("Junie", "JetBrains")
                }
                1 if id
                    .eq_ignore_ascii_case("com.github.continuedev.continueintellijextension")
                    || display.eq_ignore_ascii_case("continue") =>
                {
                    ("Continue", "Continue")
                }
                2 if display.eq_ignore_ascii_case("kilo code") => ("Kilo Code", "Kilo"),
                _ => continue,
            };
            let path_hash = hash_path(&plugin.path().to_string_lossy());
            let mut asset = Asset::new(
                sha256_digest(format!("jetbrains_plugin:{name}:{path_hash}")),
                "application",
                name,
                vendor,
                false,
            );
            asset.path_hash = Some(path_hash);
            asset.version = version;
            asset.metadata.insert("package".into(), id.into());
            asset
                .metadata
                .insert("configured_in".into(), "JetBrains IDE".into());
            asset
                .metadata
                .insert("discovery_source".into(), "Editor plugin inventory".into());
            assets.push(asset);
        }
    }
    assets.sort_by(|a, b| a.fingerprint.cmp(&b.fingerprint));
    assets.dedup_by(|a, b| a.fingerprint == b.fingerprint);
    assets
}

#[cfg(target_os = "macos")]
fn read_jar_manifest(plugin_dir: &std::fs::File, plugin_path: &Path) -> Option<Vec<u8>> {
    use crate::safe_metadata::{open_child, read_regular};
    use std::ffi::OsStr;
    let lib = open_child(plugin_dir, OsStr::new("lib"), libc::O_DIRECTORY)?;
    let jars = std::fs::read_dir(plugin_path.join("lib")).ok()?;
    let mut total = 0usize;
    for entry in jars.take(32).flatten() {
        if entry.path().extension() != Some(OsStr::new("jar")) {
            continue;
        }
        let Some(bytes) = read_regular(&lib, &entry.file_name(), 64 * 1024 * 1024) else {
            continue;
        };
        total += bytes.len();
        if total > 64 * 1024 * 1024 {
            break;
        }
        if let Some(xml) = crate::jar_manifest::plugin_xml(&bytes) {
            return Some(xml);
        }
    }
    None
}

#[cfg(target_os = "macos")]
fn parse_identity(bytes: &[u8]) -> Option<(String, String, Option<String>)> {
    let xml = std::str::from_utf8(bytes).ok()?;
    if !xml.contains("<idea-plugin") || xml.contains("<!") {
        return None;
    }
    let field = |tag: &str| -> Option<String> {
        let pattern = format!(r"(?s)<{tag}>\s*([^<]+?)\s*</{tag}>");
        let value = regex::Regex::new(&pattern)
            .ok()?
            .captures(xml)?
            .get(1)?
            .as_str()
            .trim();
        (!value.is_empty() && !value.contains('&') && !value.chars().any(char::is_control))
            .then(|| value.to_owned())
    };
    let name = field("name")?;
    let id = field("id").unwrap_or_else(|| name.clone());
    if name.len() > 255 || id.len() > 255 || id.contains(['/', '\\']) {
        return None;
    }
    let version = field("version").filter(|v| v.len() <= 128);
    Some((id, name, version))
}
