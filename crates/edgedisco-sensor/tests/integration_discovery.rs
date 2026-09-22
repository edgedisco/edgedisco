use edgedisco_sensor::discovery::{classify_process, scan_processes};
use edgedisco_sensor::process::ProcessObservation;

#[test]
fn test_classify_and_scan_known_ai_tools() {
    let test_cases = vec![
        (
            ProcessObservation::new(
                Some(100),
                Some(1),
                "/Applications/Cursor.app/Contents/MacOS/Cursor",
                vec!["/Applications/Cursor.app/Contents/MacOS/Cursor".into()],
            ),
            "Cursor",
            "Anysphere",
            "process",
        ),
        (
            ProcessObservation::new(
                Some(101),
                Some(1),
                "/usr/local/bin/ollama",
                vec!["ollama".into(), "serve".into()],
            ),
            "Ollama",
            "Ollama",
            "process",
        ),
        (
            ProcessObservation::new(
                Some(102),
                Some(1),
                "/usr/local/bin/windsurf",
                vec!["windsurf".into()],
            ),
            "Windsurf",
            "Codeium",
            "process",
        ),
        (
            ProcessObservation::new(
                Some(103),
                Some(1),
                "/Applications/LM Studio.app/Contents/MacOS/LM Studio",
                vec!["LM Studio".into()],
            ),
            "LM Studio",
            "LM Studio",
            "process",
        ),
        (
            ProcessObservation::new(
                Some(104),
                Some(1),
                "/usr/local/bin/node",
                vec![
                    "node".into(),
                    "/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js".into(),
                ],
            ),
            "Claude Code",
            "Anthropic",
            "process",
        ),
        (
            ProcessObservation::new(
                Some(105),
                Some(1),
                "/usr/local/bin/python3",
                vec!["python3".into(), "-m".into(), "crewai.cli".into()],
            ),
            "CrewAI",
            "CrewAI",
            "process",
        ),
        (
            ProcessObservation::new(
                Some(106),
                Some(1),
                "/usr/bin/python3",
                vec!["python3".into(), "-m".into(), "autogen.agent".into()],
            ),
            "AutoGen",
            "Microsoft",
            "process",
        ),
        (
            ProcessObservation::new(
                Some(107),
                Some(1),
                "/usr/local/bin/npx",
                vec![
                    "npx".into(),
                    "-y".into(),
                    "@modelcontextprotocol/server-postgres".into(),
                    "postgresql://localhost/db".into(),
                ],
            ),
            "MCP Server",
            "Unknown",
            "process",
        ),
    ];

    let observations: Vec<ProcessObservation> = test_cases
        .iter()
        .map(|(obs, _, _, _)| obs.clone())
        .collect();
    let assets = scan_processes(&observations);

    for (obs, expected_name, expected_vendor, _) in &test_cases {
        let classified = classify_process(obs).expect("should classify AI process");
        assert_eq!(classified.0, *expected_name);
        assert_eq!(classified.1, *expected_vendor);

        let asset = assets
            .iter()
            .find(|a| a.name == *expected_name && a.kind == "process")
            .unwrap_or_else(|| panic!("Asset for {expected_name} should be found"));
        assert_eq!(asset.vendor, *expected_vendor);
        assert!(asset.running);
        assert_eq!(asset.present, Some(true));
        assert!(asset.path_hash.is_some());
        assert!(asset.command_hash.is_some());
    }
}

#[test]
fn test_negative_filtering_standard_os_and_dev_tools() {
    let negative_cases = vec![
        ProcessObservation::new(
            Some(200),
            Some(1),
            "/bin/zsh",
            vec!["zsh".into(), "-l".into()],
        ),
        ProcessObservation::new(
            Some(201),
            Some(200),
            "/usr/bin/git",
            vec![
                "git".into(),
                "commit".into(),
                "-m".into(),
                "add cursor support".into(),
            ],
        ),
        ProcessObservation::new(
            Some(202),
            Some(200),
            "/Users/markcastillo/.cargo/bin/cargo",
            vec!["cargo".into(), "test".into(), "--workspace".into()],
        ),
        ProcessObservation::new(
            Some(203),
            Some(202),
            "/Users/markcastillo/.rustup/toolchains/stable/bin/rustc",
            vec!["rustc".into(), "main.rs".into()],
        ),
        ProcessObservation::new(
            Some(204),
            Some(1),
            "/usr/sbin/sshd",
            vec!["sshd".into(), "-D".into()],
        ),
        ProcessObservation::new(
            Some(205),
            Some(200),
            "/usr/bin/vim",
            vec!["vim".into(), "Cargo.toml".into()],
        ),
        ProcessObservation::new(
            Some(206),
            Some(200),
            "/bin/ps",
            vec!["ps".into(), "-ef".into()],
        ),
        ProcessObservation::new(
            Some(207),
            Some(200),
            "/usr/local/bin/python3",
            vec![
                "python3".into(),
                "-m".into(),
                "http.server".into(),
                "8000".into(),
            ],
        ),
        ProcessObservation::new(
            Some(208),
            Some(200),
            "/usr/local/bin/node",
            vec!["node".into(), "server.js".into()],
        ),
    ];

    for obs in &negative_cases {
        let classified = classify_process(obs);
        assert!(
            classified.is_none(),
            "Standard utility {:?} should not be classified, but got {:?}",
            obs.executable,
            classified
        );
    }

    let assets = scan_processes(&negative_cases);
    assert!(
        assets.is_empty(),
        "Negative cases should produce 0 discovered assets, got: {assets:?}"
    );
}

#[test]
fn test_lineage_tracking_and_agent_runtime_host_apps() {
    let observations = vec![
        // Host IDE: Cursor (PID 1000)
        ProcessObservation::new(
            Some(1000),
            Some(1),
            "/Applications/Cursor.app/Contents/MacOS/Cursor",
            vec!["/Applications/Cursor.app/Contents/MacOS/Cursor".into()],
        ),
        // Child spawned by Cursor: Claude Code agent (PID 1001)
        ProcessObservation::new(
            Some(1001),
            Some(1000),
            "/usr/local/bin/node",
            vec![
                "node".into(),
                "/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js".into(),
            ],
        ),
        // Child spawned by Cursor: MCP filesystem server (PID 1002)
        ProcessObservation::new(
            Some(1002),
            Some(1000),
            "/usr/local/bin/npx",
            vec![
                "npx".into(),
                "-y".into(),
                "@modelcontextprotocol/server-filesystem".into(),
                "/workspace".into(),
            ],
        ),
        // Direct terminal runtime: LangChain worker (PID 2000)
        ProcessObservation::new(
            Some(2000),
            Some(1),
            "/usr/local/bin/python3",
            vec!["python3".into(), "-m".into(), "langchain.worker".into()],
        ),
    ];

    let assets = scan_processes(&observations);

    // Verify Claude Code runtime asset has host_app = Cursor and relationship = spawned_by
    let claude_runtime = assets
        .iter()
        .find(|a| a.name == "Claude Code" && a.kind == "agent_runtime")
        .expect("Claude Code runtime asset should exist");

    assert_eq!(
        claude_runtime.metadata.get("host_app"),
        Some(&serde_json::Value::String("Cursor".into()))
    );
    assert_eq!(
        claude_runtime.metadata.get("relationship"),
        Some(&serde_json::Value::String("spawned_by".into()))
    );

    // Verify MCP Server runtime asset has host_app = Cursor and relationship = spawned_by
    let mcp_runtime = assets
        .iter()
        .find(|a| a.name == "MCP Server" && a.kind == "agent_runtime")
        .expect("MCP Server runtime asset should exist");

    assert_eq!(
        mcp_runtime.metadata.get("host_app"),
        Some(&serde_json::Value::String("Cursor".into()))
    );
    assert_eq!(
        mcp_runtime.metadata.get("relationship"),
        Some(&serde_json::Value::String("spawned_by".into()))
    );

    // Verify LangGraph/LangChain runtime asset has host_app = Direct/local and relationship = local_process
    let langchain_runtime = assets
        .iter()
        .find(|a| a.name == "LangGraph/LangChain" && a.kind == "agent_runtime")
        .expect("LangGraph/LangChain runtime asset should exist");

    assert_eq!(
        langchain_runtime.metadata.get("host_app"),
        Some(&serde_json::Value::String("Direct/local".into()))
    );
    assert_eq!(
        langchain_runtime.metadata.get("relationship"),
        Some(&serde_json::Value::String("local_process".into()))
    );
}

#[test]
fn test_multi_instance_aggregation() {
    let observations = vec![
        ProcessObservation::new(
            Some(100),
            Some(1),
            "/Applications/Cursor.app/Contents/MacOS/Cursor",
            vec!["Cursor".into()],
        ),
        ProcessObservation::new(
            Some(101),
            Some(100),
            "/usr/local/bin/node",
            vec!["node".into(), "@anthropic-ai/claude-code".into()],
        ),
        ProcessObservation::new(
            Some(102),
            Some(100),
            "/usr/local/bin/node",
            vec!["node".into(), "@anthropic-ai/claude-code".into()],
        ),
        ProcessObservation::new(
            Some(103),
            Some(100),
            "/usr/local/bin/node",
            vec!["node".into(), "@anthropic-ai/claude-code".into()],
        ),
    ];

    let assets = scan_processes(&observations);
    let claude_runtime = assets
        .iter()
        .find(|a| a.name == "Claude Code" && a.kind == "agent_runtime")
        .expect("Claude Code runtime asset should exist");

    assert_eq!(
        claude_runtime.metadata.get("instance_count"),
        Some(&serde_json::Value::Number(3.into()))
    );
}

#[test]
fn test_privacy_preservation_in_command_hash() {
    let secret_arg = "--api-key=sk-ant-api03-SECRET_TOKEN_VALUE_HERE";
    let prompt_arg = "Please write an exploit for CVE-2026-9999";

    let obs = ProcessObservation::new(
        Some(500),
        Some(1),
        "/usr/local/bin/claude",
        vec![
            "/usr/local/bin/claude".into(),
            secret_arg.into(),
            prompt_arg.into(),
        ],
    );

    let assets = scan_processes(&[obs]);
    assert_eq!(assets.len(), 2); // 1 process + 1 agent_runtime

    for asset in &assets {
        let meta_str = serde_json::to_string(&asset.metadata).unwrap();
        assert!(!meta_str.contains("SECRET_TOKEN_VALUE"));
        assert!(!meta_str.contains("CVE-2026-9999"));
        assert!(!meta_str.contains("exploit"));
    }
}
