use edgedisco_core::catalog::{
    agent_by_executable, agent_by_name, catalog, classify_process_name, classify_text,
    contains_signature, is_agent_runtime, is_host_app, known_binary,
};

#[test]
fn test_catalog_integrity() {
    let cat = catalog();
    assert_eq!(cat.schema_version, 1);
    assert_eq!(cat.hash_algorithm, "sha256");
    assert!(!cat.updated.is_empty());
    assert!(cat.agents.len() >= 15);

    // Ensure all agent names and vendors are non-empty
    for agent in &cat.agents {
        assert!(!agent.name.is_empty(), "Agent name must not be empty");
        assert!(!agent.vendor.is_empty(), "Agent vendor must not be empty");
    }
}

#[test]
fn test_agent_lookups() {
    let claude = agent_by_name("Claude Code").expect("find Claude Code");
    assert_eq!(claude.vendor, "Anthropic");
    assert!(claude.executables.contains(&"claude".to_string()));

    let codex = agent_by_executable("codex").expect("find by codex exe");
    assert_eq!(codex.name, "OpenAI Codex");
    assert_eq!(codex.vendor, "OpenAI");

    let hermes = agent_by_name("Hermes Agent").expect("find Hermes Agent");
    assert_eq!(hermes.vendor, "Nous Research");
    assert!(hermes.executables.contains(&"hermes".to_string()));
}

#[test]
fn test_binary_fingerprint_matching() {
    let binary = known_binary(
        "Factory Droid",
        "efed63e905bcc6f7e17d9deaf6542b4a4d5cff7dcb3a7a4315a31c471050f925",
    );
    assert!(binary.is_some());
    let b = binary.unwrap();
    assert_eq!(b.version, "0.223.0");
    assert_eq!(b.platform, "darwin");
    assert_eq!(b.architecture, "arm64");

    // Negative case: unknown hash
    let unknown = known_binary(
        "Factory Droid",
        "0000000000000000000000000000000000000000000000000000000000000000",
    );
    assert!(unknown.is_none());
}

#[test]
fn test_text_and_process_classifications() {
    let cases = [
        ("claude", "Claude Code", "Anthropic"),
        ("cursor", "Cursor", "Anysphere"),
        ("ollama", "Ollama", "Ollama"),
        ("vibe", "Mistral Vibe", "Mistral AI"),
        ("droid", "Factory Droid", "Factory"),
        ("hermes", "Hermes Agent", "Nous Research"),
        ("windsurf", "Windsurf", "Codeium"),
    ];

    for (exe, expected_name, expected_vendor) in cases {
        let (name, vendor) =
            classify_process_name(exe).unwrap_or_else(|| panic!("failed to classify {exe}"));
        assert_eq!(name, expected_name);
        assert_eq!(vendor, expected_vendor);
    }
}

#[test]
fn test_negative_classifications() {
    assert!(classify_process_name("bash").is_none());
    assert!(classify_process_name("zsh").is_none());
    assert!(classify_process_name("python").is_none());
    assert!(classify_process_name("node").is_none());
    assert!(classify_text("standard enterprise text document").is_none());
}

#[test]
fn test_host_app_and_runtime_predicates() {
    assert!(is_host_app("Cursor"));
    assert!(is_host_app("Claude"));
    assert!(is_host_app("ChatGPT"));
    assert!(is_host_app("LM Studio"));

    assert!(is_agent_runtime("CrewAI"));
    assert!(is_agent_runtime("AutoGen"));
    assert!(is_agent_runtime("LangGraph/LangChain"));
    assert!(is_agent_runtime("MCP Server"));
    assert!(is_agent_runtime("Claude Code"));

    assert!(!is_host_app("Safari"));
    assert!(!is_agent_runtime("Safari"));
}

#[test]
fn test_token_boundary_precision() {
    assert!(contains_signature("running cursor today", "cursor"));
    assert!(contains_signature("/path/to/cursor", "cursor"));
    assert!(contains_signature("cursor-agent", "cursor-agent"));
    assert!(contains_signature("cursor_is_awesome", "cursor"));
    assert!(!contains_signature("cursorfoo", "cursor"));
    assert!(!contains_signature("precursor", "cursor"));
}
