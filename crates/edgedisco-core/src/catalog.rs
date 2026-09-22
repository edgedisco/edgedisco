use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::sync::OnceLock;

pub const CATALOG_RESOURCE_JSON: &str =
    include_str!("../../../src/ai_asset_inventory/fingerprints.json");

/// Known binary fingerprint entry with cryptographic SHA-256 hash.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BinaryFingerprint {
    pub sha256: String,
    pub version: String,
    pub platform: String,
    pub architecture: String,
    pub source: String,
}

/// AI tool or agent fingerprint definition.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AgentFingerprint {
    pub name: String,
    pub vendor: String,
    #[serde(default)]
    pub executables: Vec<String>,
    #[serde(default)]
    pub package_markers: Vec<String>,
    #[serde(default)]
    pub display_markers: Vec<String>,
    #[serde(default)]
    pub sources: Vec<String>,
    #[serde(default)]
    pub binary_fingerprints: Vec<BinaryFingerprint>,
}

/// Catalog structure deserialized from fingerprints.json.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FingerprintCatalog {
    pub schema_version: u32,
    pub updated: String,
    pub hash_algorithm: String,
    pub agents: Vec<AgentFingerprint>,
}

/// Application signature rule (Name, Vendor, Needle patterns).
pub struct ApplicationSignature {
    pub name: &'static str,
    pub vendor: &'static str,
    pub needles: &'static [&'static str],
}

pub const APPLICATION_SIGNATURES: &[ApplicationSignature] = &[
    ApplicationSignature {
        name: "Google Antigravity IDE",
        vendor: "Google",
        needles: &["antigravity ide", "agy-ide"],
    },
    ApplicationSignature {
        name: "Google Antigravity",
        vendor: "Google",
        needles: &["google antigravity", "antigravity"],
    },
    ApplicationSignature {
        name: "Kiro IDE",
        vendor: "AWS",
        needles: &["kiro", "kiro ide"],
    },
    ApplicationSignature {
        name: "ChatGPT",
        vendor: "OpenAI",
        needles: &["chatgpt", "openai.chat"],
    },
    ApplicationSignature {
        name: "Claude",
        vendor: "Anthropic",
        needles: &["claude", "anthropic"],
    },
    ApplicationSignature {
        name: "Cursor",
        vendor: "Anysphere",
        needles: &["cursor"],
    },
    ApplicationSignature {
        name: "GitHub Copilot",
        vendor: "GitHub",
        needles: &["github copilot", "copilot-agent", "copilot chat"],
    },
    ApplicationSignature {
        name: "Windsurf",
        vendor: "Codeium",
        needles: &["windsurf", "codeium"],
    },
    ApplicationSignature {
        name: "Ollama",
        vendor: "Ollama",
        needles: &["ollama"],
    },
    ApplicationSignature {
        name: "LM Studio",
        vendor: "LM Studio",
        needles: &["lm studio", "lmstudio"],
    },
    ApplicationSignature {
        name: "Jan",
        vendor: "Jan",
        needles: &["jan.app", "jan.exe", "/jan"],
    },
    ApplicationSignature {
        name: "AnythingLLM",
        vendor: "Mintplex Labs",
        needles: &["anythingllm"],
    },
    ApplicationSignature {
        name: "Open WebUI",
        vendor: "Open WebUI",
        needles: &["open-webui", "open_webui"],
    },
    ApplicationSignature {
        name: "LocalAI",
        vendor: "LocalAI",
        needles: &["local-ai", "localai"],
    },
    ApplicationSignature {
        name: "Dify",
        vendor: "Dify",
        needles: &["dify"],
    },
    ApplicationSignature {
        name: "CrewAI",
        vendor: "CrewAI",
        needles: &["crewai"],
    },
    ApplicationSignature {
        name: "AutoGen",
        vendor: "Microsoft",
        needles: &["autogen", "agentchat"],
    },
    ApplicationSignature {
        name: "LangGraph/LangChain",
        vendor: "LangChain",
        needles: &["langgraph", "langchain"],
    },
    ApplicationSignature {
        name: "MCP Server",
        vendor: "Unknown",
        needles: &[
            "mcp-server",
            "mcp_server",
            "@modelcontextprotocol",
            "server-filesystem",
            "server-postgres",
            "server-github",
        ],
    },
];

pub struct ExtensionSignature {
    pub name: &'static str,
    pub vendor: &'static str,
    pub needles: &'static [&'static str],
}

pub const VSCODE_EXTENSION_SIGNATURES: &[ExtensionSignature] = &[
    ExtensionSignature {
        name: "Claude Code",
        vendor: "Anthropic",
        needles: &["anthropic.claude-code"],
    },
    ExtensionSignature {
        name: "OpenAI Codex",
        vendor: "OpenAI",
        needles: &["openai.chatgpt"],
    },
    ExtensionSignature {
        name: "Cline",
        vendor: "Cline",
        needles: &["saoudrizwan.claude-dev"],
    },
    ExtensionSignature {
        name: "Continue",
        vendor: "Continue",
        needles: &["continue.continue"],
    },
    ExtensionSignature {
        name: "Kilo Code",
        vendor: "Kilo",
        needles: &["kilocode.kilo-code"],
    },
    ExtensionSignature {
        name: "Kimi Code",
        vendor: "Moonshot AI",
        needles: &["moonshot-ai.kimi-code"],
    },
    ExtensionSignature {
        name: "Augment Code",
        vendor: "Augment Code",
        needles: &["augment.vscode-augment"],
    },
    ExtensionSignature {
        name: "GitHub Copilot",
        vendor: "GitHub",
        needles: &["github.copilot", "github.copilot-chat"],
    },
    ExtensionSignature {
        name: "Gemini Code Assist",
        vendor: "Google",
        needles: &["google.geminicodeassist"],
    },
    ExtensionSignature {
        name: "OpenCode",
        vendor: "Anomaly",
        needles: &["sst-dev.opencode"],
    },
];

static CATALOG: OnceLock<FingerprintCatalog> = OnceLock::new();
static AGENT_NAMES: OnceLock<HashSet<String>> = OnceLock::new();
static HOST_NAMES: OnceLock<HashSet<String>> = OnceLock::new();

/// Return the globally loaded fingerprint catalog.
pub fn catalog() -> &'static FingerprintCatalog {
    CATALOG.get_or_init(|| {
        serde_json::from_str(CATALOG_RESOURCE_JSON)
            .expect("embedded fingerprints.json must be valid JSON")
    })
}

/// Check if a text contains a signature needle matching token boundaries for alphanumeric keywords.
pub fn contains_signature(text: &str, needle: &str) -> bool {
    let lowered_text = text.to_lowercase();
    let lowered_needle = needle.to_lowercase();

    let is_alnum = lowered_needle.chars().all(|c| c.is_ascii_alphanumeric());
    if !is_alnum {
        return lowered_text.contains(&lowered_needle);
    }

    let needle_len = lowered_needle.len();
    let text_bytes = lowered_text.as_bytes();

    if needle_len == 0 || text_bytes.len() < needle_len {
        return false;
    }

    let mut start = 0;
    while let Some(pos) = lowered_text[start..].find(&lowered_needle) {
        let match_idx = start + pos;
        let char_before_ok = if match_idx == 0 {
            true
        } else {
            !text_bytes[match_idx - 1].is_ascii_alphanumeric()
        };

        let char_after_ok = if match_idx + needle_len >= text_bytes.len() {
            true
        } else {
            !text_bytes[match_idx + needle_len].is_ascii_alphanumeric()
        };

        if char_before_ok && char_after_ok {
            return true;
        }

        start = match_idx + 1;
        if start >= text_bytes.len() {
            break;
        }
    }

    false
}

/// Classify a string against all catalog agents and application signatures.
pub fn classify_text(text: &str) -> Option<(&'static str, &'static str)> {
    let cat = catalog();
    for agent in &cat.agents {
        for marker in &agent.display_markers {
            if contains_signature(text, marker) {
                return Some((agent.name.as_str(), agent.vendor.as_str()));
            }
        }
    }

    for app in APPLICATION_SIGNATURES {
        for needle in app.needles {
            if contains_signature(text, needle) {
                return Some((app.name, app.vendor));
            }
        }
    }

    None
}

/// Classify a process executable name or command token against agent definitions.
pub fn classify_process_name(name: &str) -> Option<(&'static str, &'static str)> {
    let lowered = name.to_lowercase();
    let cat = catalog();

    for agent in &cat.agents {
        for exe in &agent.executables {
            if exe.to_lowercase() == lowered {
                return Some((agent.name.as_str(), agent.vendor.as_str()));
            }
        }
    }

    for app in APPLICATION_SIGNATURES {
        for needle in app.needles {
            if contains_signature(&lowered, needle) {
                return Some((app.name, app.vendor));
            }
        }
    }

    None
}

/// Find a known binary fingerprint in the catalog by agent name and SHA-256 hash.
pub fn known_binary(name: &str, sha256: &str) -> Option<&'static BinaryFingerprint> {
    let cat = catalog();
    for agent in &cat.agents {
        if agent.name.eq_ignore_ascii_case(name) {
            for binary in &agent.binary_fingerprints {
                if binary.sha256.eq_ignore_ascii_case(sha256) {
                    return Some(binary);
                }
            }
        }
    }
    None
}

/// Look up an agent fingerprint entry by exact name.
pub fn agent_by_name(name: &str) -> Option<&'static AgentFingerprint> {
    catalog()
        .agents
        .iter()
        .find(|a| a.name.eq_ignore_ascii_case(name))
}

/// Look up an agent fingerprint entry by executable name.
pub fn agent_by_executable(exe: &str) -> Option<&'static AgentFingerprint> {
    let lowered = exe.to_lowercase();
    catalog()
        .agents
        .iter()
        .find(|a| a.executables.iter().any(|e| e.to_lowercase() == lowered))
}

/// Check if an asset name represents an agent runtime.
pub fn is_agent_runtime(name: &str) -> bool {
    let set = AGENT_NAMES.get_or_init(|| {
        let mut set = HashSet::new();
        set.insert("CrewAI".to_string());
        set.insert("AutoGen".to_string());
        set.insert("LangGraph/LangChain".to_string());
        set.insert("MCP Server".to_string());
        set.insert("Dify".to_string());
        for agent in &catalog().agents {
            set.insert(agent.name.clone());
        }
        set
    });
    set.contains(name)
}

/// Check if an asset name represents a known host application.
pub fn is_host_app(name: &str) -> bool {
    let set = HOST_NAMES.get_or_init(|| {
        let mut set = HashSet::new();
        set.insert("ChatGPT".to_string());
        set.insert("Claude".to_string());
        set.insert("Cursor".to_string());
        set.insert("GitHub Copilot".to_string());
        set.insert("Windsurf".to_string());
        set.insert("LM Studio".to_string());
        set.insert("AnythingLLM".to_string());
        set.insert("Open WebUI".to_string());
        set.insert("Google Antigravity".to_string());
        set.insert("Kiro IDE".to_string());
        set.insert("CrewAI".to_string());
        set.insert("AutoGen".to_string());
        set.insert("LangGraph/LangChain".to_string());
        set.insert("MCP Server".to_string());
        set.insert("Dify".to_string());
        for agent in &catalog().agents {
            set.insert(agent.name.clone());
        }
        set
    });
    set.contains(name)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_catalog_loaded() {
        let cat = catalog();
        assert_eq!(cat.schema_version, 1);
        assert_eq!(cat.hash_algorithm, "sha256");
        assert!(!cat.agents.is_empty());
    }

    #[test]
    fn test_contains_signature_token_boundary() {
        assert!(contains_signature(
            "running claude code in terminal",
            "claude code"
        ));
        assert!(contains_signature("cursor", "cursor"));
        assert!(!contains_signature("precursor", "cursor"));
        assert!(!contains_signature("cursorfoo", "cursor"));
        assert!(contains_signature("cursor-agent", "cursor-agent"));
    }

    #[test]
    fn test_classify_text() {
        assert_eq!(
            classify_text("Anthropic Claude Code is running"),
            Some(("Claude Code", "Anthropic"))
        );
        assert_eq!(
            classify_text("Using cursor editor"),
            Some(("Cursor", "Anysphere"))
        );
        assert_eq!(
            classify_text("Ollama local LLM daemon"),
            Some(("Ollama", "Ollama"))
        );
        assert_eq!(classify_text("unknown non-ai binary"), None);
    }

    #[test]
    fn test_classify_process_name() {
        assert_eq!(
            classify_process_name("claude"),
            Some(("Claude Code", "Anthropic"))
        );
        assert_eq!(
            classify_process_name("droid"),
            Some(("Factory Droid", "Factory"))
        );
        assert_eq!(classify_process_name("ollama"), Some(("Ollama", "Ollama")));
    }

    #[test]
    fn test_known_binary() {
        let binary = known_binary(
            "Factory Droid",
            "efed63e905bcc6f7e17d9deaf6542b4a4d5cff7dcb3a7a4315a31c471050f925",
        );
        assert!(binary.is_some());
        let b = binary.unwrap();
        assert_eq!(b.version, "0.223.0");
        assert_eq!(b.platform, "darwin");
        assert_eq!(b.architecture, "arm64");
    }

    #[test]
    fn test_host_and_agent_names() {
        assert!(is_host_app("Cursor"));
        assert!(is_host_app("Claude Code"));
        assert!(is_agent_runtime("Claude Code"));
        assert!(is_agent_runtime("CrewAI"));
        assert!(!is_agent_runtime("NotAnAgent"));
    }
}
