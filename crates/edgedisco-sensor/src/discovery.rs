use crate::process::ProcessObservation;
use edgedisco_core::catalog::{
    agent_by_executable, catalog, classify_process_name, classify_text, is_agent_runtime,
    is_host_app, known_binary,
};
use edgedisco_core::models::Asset;
use edgedisco_core::redaction::{
    hash_path, identity_token_index, safe_command_fingerprint, sha256_digest,
};
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

/// Classify observed processes and map them against the AI detection catalog into Asset objects.
pub fn scan_processes(observations: &[ProcessObservation]) -> Vec<Asset> {
    let mut found: HashMap<String, Asset> = HashMap::new();
    let mut classifications: HashMap<u32, (&'static str, &'static str)> = HashMap::new();

    for row in observations {
        if let Some(classified) = classify_process(row) {
            if let Some(pid) = row.pid {
                classifications.insert(pid, classified);
            }
        }
    }

    for row in observations {
        let classified = row
            .pid
            .and_then(|pid| classifications.get(&pid).copied())
            .or_else(|| classify_process(row));

        let (name, vendor) = match classified {
            Some(c) => c,
            None => continue,
        };

        let path_hash = hash_path(&row.executable);
        let command_hash = compute_command_hash(row);
        let binary_path = process_binary_candidate(row);
        let (binary_sha256, binary_status) = binary_evidence(name, binary_path.as_deref());

        let fp = sha256_digest(format!("process:{name}:{path_hash}"));
        let executable_name = path_name(&row.executable);

        let mut metadata = HashMap::new();
        metadata.insert(
            "executable".to_string(),
            serde_json::Value::String(executable_name),
        );

        let library_version = if binary_sha256.is_some() {
            Some(catalog().updated.clone())
        } else {
            None
        };

        found.insert(
            fp.clone(),
            Asset {
                fingerprint: fp,
                kind: "process".to_string(),
                name: name.to_string(),
                vendor: vendor.to_string(),
                running: true,
                present: Some(true),
                version: None,
                path_hash: Some(path_hash),
                command_hash: Some(command_hash),
                binary_sha256,
                binary_fingerprint_status: binary_status,
                fingerprint_library_version: library_version,
                metadata,
                first_seen: None,
                last_seen: None,
            },
        );
    }

    let runtimes = agent_runtime_assets(observations, &classifications);
    for (fp, asset) in runtimes {
        found.insert(fp, asset);
    }

    let mut result: Vec<Asset> = found.into_values().collect();
    result.sort_by(|a, b| a.fingerprint.cmp(&b.fingerprint));
    result
}

/// Classify a single process observation against catalog signatures.
pub fn classify_process(row: &ProcessObservation) -> Option<(&'static str, &'static str)> {
    let executable_name = path_name(&row.executable).to_lowercase();
    let leading = args_leading_executable_name(&row.args)
        .unwrap_or_default()
        .to_lowercase();
    let effective = if is_generic_runtime_name(&leading) {
        leading.clone()
    } else {
        executable_name.clone()
    };

    let cat = catalog();
    for agent in &cat.agents {
        let matches_exe = agent.executables.iter().any(|exe| {
            let exe_lower = exe.to_lowercase();
            effective == exe_lower || executable_name == exe_lower || leading == exe_lower
        });
        if matches_exe {
            if agent.name == "Factory Droid" {
                let p = Path::new(&row.executable);
                if !p.is_absolute() {
                    return None;
                }
                let (_, status) = binary_evidence(&agent.name, Some(p));
                if status.as_deref() != Some("matched") {
                    return None;
                }
            }
            return Some((agent.name.as_str(), agent.vendor.as_str()));
        }
    }

    if is_generic_runtime_name(&effective) {
        let command = process_identity_text(row);
        let identity = if let Some((_, rest)) = command.split_once(' ') {
            rest
        } else {
            ""
        };
        let runner = matches!(effective.as_str(), "npx" | "npx.exe" | "uvx");
        let package = if runner {
            runner_package(identity)
        } else {
            String::new()
        };

        for agent in &cat.agents {
            if agent.name == "Factory Droid" {
                continue;
            }
            let identity_basename = path_name(identity);
            let console_script = !runner
                && effective != "docker"
                && agent
                    .executables
                    .iter()
                    .any(|e| identity_basename.eq_ignore_ascii_case(e));

            let package_match = if runner {
                agent
                    .package_markers
                    .iter()
                    .any(|m| runner_marker_matches(&package, m))
            } else {
                agent
                    .package_markers
                    .iter()
                    .any(|m| identity_marker_matches(identity, m))
            };

            if console_script || package_match {
                return Some((agent.name.as_str(), agent.vendor.as_str()));
            }
        }

        if let Some((name, vendor)) = classify_text(&command) {
            if matches!(
                name,
                "CrewAI" | "AutoGen" | "LangGraph/LangChain" | "MCP Server" | "Dify"
            ) {
                return Some((name, vendor));
            }
        }
        return None;
    }

    classify_process_name(&row.executable)
}

/// Extract clean basename handling both POSIX `/` and Windows `\` path separators.
pub fn path_name(path: &str) -> String {
    if let Some(idx) = path.rfind('\\') {
        path[idx + 1..].to_string()
    } else {
        Path::new(path)
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or(path)
            .to_string()
    }
}

/// Extract leading executable name from arguments if present.
pub fn args_leading_executable_name(args: &[String]) -> Option<String> {
    if args.is_empty() {
        return None;
    }
    let first = args[0].trim().trim_matches(|c| c == '\'' || c == '"');
    if first.is_empty() {
        return None;
    }
    let token = first
        .split_whitespace()
        .next()
        .unwrap_or(first)
        .trim_matches(|c| c == '\'' || c == '"');
    if token.is_empty() {
        return None;
    }
    Some(path_name(token))
}

/// Determine if an executable name is a generic script runner or container runtime.
pub fn is_generic_runtime_name(name: &str) -> bool {
    let lowered = name.to_lowercase();
    if matches!(
        lowered.as_str(),
        "python" | "python3" | "node" | "node.exe" | "npx" | "npx.exe" | "uvx" | "docker"
    ) {
        return true;
    }
    if let Some(rest) = lowered.strip_prefix("python") {
        if !rest.is_empty() && rest.chars().all(|c| c.is_ascii_digit() || c == '.') {
            return true;
        }
    }
    false
}

/// Extract process identity without prompts or arguments.
pub fn process_identity_text(row: &ProcessObservation) -> String {
    let parts: Vec<String> = if row.args.len() == 1 {
        if let Some(split) = shlex::split(&row.args[0]) {
            split
        } else {
            row.args[0]
                .split_whitespace()
                .map(|s| s.to_string())
                .collect()
        }
    } else {
        row.args.clone()
    };

    let executable = path_name(&row.executable).to_lowercase();
    if parts.is_empty() {
        return executable;
    }

    let leading = path_name(&parts[0]).to_lowercase();
    let runtime = if is_generic_runtime_name(&leading) {
        leading
    } else {
        executable
    };

    let mut identity = vec![runtime.clone()];
    let parts_str: Vec<&str> = parts.iter().map(|s| s.as_str()).collect();
    if let Some(idx) = identity_token_index(&parts_str, &runtime) {
        if idx < parts.len() {
            identity.push(parts[idx].clone());
        }
    }
    identity.join(" ").to_lowercase().replace('\\', "/")
}

/// Normalize an npx/uvx package spec while preserving an npm scope.
pub fn runner_package(identity: &str) -> String {
    let mut value = identity.to_lowercase().replace('\\', "/");
    if value.starts_with('@') {
        if let Some(slash_idx) = value.find('/') {
            if let Some(at_idx) = value[slash_idx + 1..].find('@') {
                value.truncate(slash_idx + 1 + at_idx);
            }
        }
        return value;
    }
    for separator in ["@", "=="] {
        if let Some((prefix, _)) = value.split_once(separator) {
            return prefix.to_string();
        }
    }
    value
}

/// Match runner package against catalog marker.
pub fn runner_marker_matches(package: &str, marker: &str) -> bool {
    let marker = marker.to_lowercase();
    let marker = marker.trim_end_matches('/');
    if marker.starts_with('/') || marker.contains("node_modules/") {
        return false;
    }
    if marker.starts_with('@') && !marker.contains('/') {
        return package.starts_with(&format!("{marker}/"));
    }
    package == marker
}

/// Match a package/module component without accepting arbitrary name prefixes.
pub fn identity_marker_matches(identity: &str, marker: &str) -> bool {
    let lowered_identity = identity.to_lowercase().replace('\\', "/");
    let lowered_marker = marker.to_lowercase();
    let marker_trimmed = lowered_marker.trim_end_matches('/');

    if marker_trimmed.is_empty() {
        return false;
    }

    let marker_len = marker_trimmed.len();
    let id_bytes = lowered_identity.as_bytes();

    if id_bytes.len() < marker_len {
        return false;
    }

    let is_ident_char = |b: u8| b.is_ascii_alphanumeric() || b == b'_' || b == b'-';

    let mut start = 0;
    while let Some(pos) = lowered_identity[start..].find(marker_trimmed) {
        let match_idx = start + pos;
        let char_before_ok = if match_idx == 0 {
            true
        } else {
            !is_ident_char(id_bytes[match_idx - 1])
        };

        let char_after_ok = if match_idx + marker_len >= id_bytes.len() {
            true
        } else {
            !is_ident_char(id_bytes[match_idx + marker_len])
        };

        if char_before_ok && char_after_ok {
            return true;
        }

        start = match_idx + 1;
        if start >= id_bytes.len() {
            break;
        }
    }

    false
}

/// Compute safe command hash preserving privacy.
pub fn compute_command_hash(row: &ProcessObservation) -> String {
    if row.args.is_empty() {
        return safe_command_fingerprint(&[&row.executable]);
    }
    if row.args.len() == 1 {
        if let Some(parts) = shlex::split(&row.args[0]) {
            let parts_ref: Vec<&str> = parts.iter().map(|s| s.as_str()).collect();
            return safe_command_fingerprint(&parts_ref);
        }
    }
    let parts_ref: Vec<&str> = row.args.iter().map(|s| s.as_str()).collect();
    safe_command_fingerprint(&parts_ref)
}

/// Locate candidate binary on disk for SHA-256 evidence hashing.
pub fn process_binary_candidate(row: &ProcessObservation) -> Option<PathBuf> {
    let exe_name = path_name(&row.executable).to_lowercase();
    let is_known = agent_by_executable(&exe_name).is_some();
    if is_known || !is_generic_runtime_name(&exe_name) {
        return Some(PathBuf::from(&row.executable));
    }
    let parts = if row.args.len() == 1 {
        shlex::split(&row.args[0]).unwrap_or_else(|| vec![row.args[0].clone()])
    } else {
        row.args.clone()
    };
    if !parts.is_empty() {
        let first_name = path_name(&parts[0]).to_lowercase();
        if agent_by_executable(&first_name).is_some() {
            return Some(PathBuf::from(&parts[0]));
        }
    }
    None
}

/// Calculate SHA-256 evidence and verify against known catalog hashes.
pub fn binary_evidence(name: &str, path: Option<&Path>) -> (Option<String>, Option<String>) {
    let path = match path {
        Some(p) if p.exists() && p.is_file() => p,
        _ => return (None, None),
    };
    if let Ok(bytes) = std::fs::read(path) {
        let sha256 = sha256_digest(&bytes);
        let status = if known_binary(name, &sha256).is_some() {
            "matched".to_string()
        } else {
            "unlisted".to_string()
        };
        (Some(sha256), Some(status))
    } else {
        (None, None)
    }
}

/// Resolve ancestor host application for agent runtimes.
pub fn host_app(
    row: &ProcessObservation,
    by_pid: &HashMap<u32, &ProcessObservation>,
    classifications: &HashMap<u32, (&'static str, &'static str)>,
    runtime_name: &str,
) -> Option<&'static str> {
    let mut parent = row.ppid;
    let mut visited = HashSet::new();

    for _ in 0..8 {
        let ppid = parent?;
        if !visited.insert(ppid) {
            return None;
        }
        if let Some(&(classified_name, _)) = classifications.get(&ppid) {
            if is_host_app(classified_name) && classified_name != runtime_name {
                return Some(classified_name);
            }
        }
        let ancestor = by_pid.get(&ppid)?;
        parent = ancestor.ppid;
    }
    None
}

/// Aggregate agent runtime processes grouped by host, runtime binary, and command fingerprint.
pub fn agent_runtime_assets(
    observations: &[ProcessObservation],
    classifications: &HashMap<u32, (&'static str, &'static str)>,
) -> HashMap<String, Asset> {
    let by_pid: HashMap<u32, &ProcessObservation> = observations
        .iter()
        .filter_map(|r| r.pid.map(|p| (p, r)))
        .collect();

    struct GroupState {
        count: u32,
        vendor: &'static str,
    }

    let mut grouped: HashMap<(String, String, String, String), GroupState> = HashMap::new();

    for row in observations {
        let pid = match row.pid {
            Some(p) => p,
            None => continue,
        };
        let (name, vendor) = match classifications.get(&pid) {
            Some(&(n, v)) if is_agent_runtime(n) => (n, v),
            _ => continue,
        };

        let host = host_app(row, &by_pid, classifications, name).unwrap_or("Direct/local");
        let executable_name = path_name(&row.executable);
        let command_hash = compute_command_hash(row);

        let key = (
            name.to_string(),
            host.to_string(),
            executable_name.to_lowercase(),
            command_hash,
        );

        let entry = grouped
            .entry(key)
            .or_insert(GroupState { count: 0, vendor });
        entry.count += 1;
    }

    let mut assets = HashMap::new();
    for ((name, host, executable_name, command_hash), state) in grouped {
        let fp = sha256_digest(format!(
            "agent_runtime:{name}:{host}:{executable_name}:{command_hash}"
        ));
        let relationship = if host != "Direct/local" {
            "spawned_by"
        } else {
            "local_process"
        };
        let mut metadata = HashMap::new();
        metadata.insert("host_app".to_string(), serde_json::Value::String(host));
        metadata.insert(
            "runtime".to_string(),
            serde_json::Value::String(executable_name),
        );
        metadata.insert(
            "instance_count".to_string(),
            serde_json::Value::Number(state.count.into()),
        );
        metadata.insert(
            "relationship".to_string(),
            serde_json::Value::String(relationship.to_string()),
        );

        let asset = Asset {
            fingerprint: fp.clone(),
            kind: "agent_runtime".to_string(),
            name,
            vendor: state.vendor.to_string(),
            running: true,
            present: Some(true),
            version: None,
            path_hash: None,
            command_hash: Some(command_hash),
            binary_sha256: None,
            binary_fingerprint_status: None,
            fingerprint_library_version: None,
            metadata,
            first_seen: None,
            last_seen: None,
        };
        assets.insert(fp, asset);
    }

    assets
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_classify_process_and_scan_discovery() {
        let obs = vec![
            ProcessObservation::new(
                Some(100),
                Some(1),
                "/Applications/Cursor.app/Contents/MacOS/Cursor",
                vec!["/Applications/Cursor.app/Contents/MacOS/Cursor".to_string()],
            ),
            ProcessObservation::new(
                Some(101),
                Some(100),
                "/usr/local/bin/node",
                vec![
                    "node".to_string(),
                    "/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js".to_string(),
                ],
            ),
            ProcessObservation::new(
                Some(200),
                Some(1),
                "/usr/local/bin/ollama",
                vec!["ollama".to_string(), "serve".to_string()],
            ),
            ProcessObservation::new(
                Some(300),
                Some(1),
                "/bin/zsh",
                vec!["zsh".to_string(), "-l".to_string()],
            ),
            ProcessObservation::new(
                Some(301),
                Some(300),
                "/usr/bin/git",
                vec!["git".to_string(), "status".to_string()],
            ),
        ];

        let assets = scan_processes(&obs);

        // Assets should include Cursor, Claude Code, and Ollama; standard utilities (zsh, git) ignored.
        let names: HashSet<String> = assets.iter().map(|a| a.name.clone()).collect();
        assert!(names.contains("Cursor"));
        assert!(names.contains("Claude Code"));
        assert!(names.contains("Ollama"));
        assert!(!names.contains("zsh"));
        assert!(!names.contains("git"));

        // Claude Code should have host_app Cursor in agent_runtime
        let claude_runtime = assets
            .iter()
            .find(|a| a.name == "Claude Code" && a.kind == "agent_runtime")
            .expect("Claude Code runtime asset should exist");
        assert_eq!(
            claude_runtime.metadata.get("host_app"),
            Some(&serde_json::Value::String("Cursor".to_string()))
        );
        assert_eq!(
            claude_runtime.metadata.get("relationship"),
            Some(&serde_json::Value::String("spawned_by".to_string()))
        );
    }
}
