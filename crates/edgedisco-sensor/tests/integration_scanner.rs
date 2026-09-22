use edgedisco_sensor::process::darwin::DarwinProcessScanner;
use edgedisco_sensor::process::fallback::{parse_ps_output, FallbackProcessScanner};
use edgedisco_sensor::process::linux::LinuxProcessScanner;
use edgedisco_sensor::process::{scan_host_processes, ProcessScanner};
use std::fs::{create_dir_all, write};
use tempfile::tempdir;

#[test]
fn test_live_host_process_scanner() {
    let result = scan_host_processes();
    assert!(
        result.is_ok(),
        "Live host process scan should succeed, got: {:?}",
        result.err()
    );

    let observations = result.unwrap();
    assert!(
        !observations.is_empty(),
        "Host process scan should return non-empty observations"
    );

    // Verify each observation has non-empty executable and args
    for obs in &observations {
        assert!(
            obs.pid.is_some(),
            "Observed process must have a pid: {obs:?}"
        );
        assert!(
            !obs.executable.is_empty(),
            "Observed process must have an executable: {obs:?}"
        );
    }
}

#[test]
fn test_darwin_process_scanner_instantiation_and_scan() {
    let scanner = DarwinProcessScanner::new();
    let result = scanner.scan();
    assert!(
        result.is_ok(),
        "Darwin process scan should succeed: {:?}",
        result.err()
    );
    let observations = result.unwrap();
    assert!(!observations.is_empty());
}

#[test]
fn test_fallback_ps_scanner_instantiation_and_scan() {
    let scanner = FallbackProcessScanner::new();
    let result = scanner.scan();
    assert!(
        result.is_ok(),
        "Fallback process scan should succeed: {:?}",
        result.err()
    );
    let observations = result.unwrap();
    assert!(!observations.is_empty());
}

#[test]
fn test_linux_proc_scanner_with_synthetic_hierarchy() {
    let temp = tempdir().expect("tempdir");
    let proc_root = temp.path();

    // Process 1: init
    let p1 = proc_root.join("1");
    create_dir_all(&p1).unwrap();
    write(
        p1.join("stat"),
        "1 (init) S 0 1 1 0 -1 4194560 1 0 0 0 0 0 0 0 20 0 1 0 0 0 0 0",
    )
    .unwrap();
    write(p1.join("comm"), "init\n").unwrap();
    write(p1.join("cmdline"), b"/sbin/init\0").unwrap();

    // Process 500: cursor IDE
    let p500 = proc_root.join("500");
    create_dir_all(&p500).unwrap();
    write(
        p500.join("stat"),
        "500 (cursor) S 1 500 500 0 -1 4194560 1 0 0 0 0 0 0 0 20 0 1 0 0 0 0 0",
    )
    .unwrap();
    write(p500.join("comm"), "cursor\n").unwrap();
    write(p500.join("cmdline"), b"/opt/cursor/cursor\0--no-sandbox\0").unwrap();

    // Process 501: claude agent
    let p501 = proc_root.join("501");
    create_dir_all(&p501).unwrap();
    write(
        p501.join("stat"),
        "501 (node) S 500 500 500 0 -1 4194560 1 0 0 0 0 0 0 0 20 0 1 0 0 0 0 0",
    )
    .unwrap();
    write(p501.join("comm"), "node\n").unwrap();
    write(
        p501.join("cmdline"),
        b"/usr/bin/node\0/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js\0",
    )
    .unwrap();

    let scanner = LinuxProcessScanner::with_root(proc_root);
    let observations = scanner.scan().expect("linux proc scan should succeed");
    assert_eq!(observations.len(), 3);

    let claude_obs = observations.iter().find(|o| o.pid == Some(501)).unwrap();
    assert_eq!(claude_obs.ppid, Some(500));
    assert_eq!(claude_obs.executable, "node");
    assert_eq!(
        claude_obs.args,
        vec![
            "/usr/bin/node",
            "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"
        ]
    );
}

#[test]
fn test_parse_ps_output_edge_cases() {
    let empty_output = "";
    assert!(parse_ps_output(empty_output).is_err());

    let whitespace_only = "   \n\t  \n";
    assert!(parse_ps_output(whitespace_only).is_err());

    let malformed_lines = "not a valid pid line\nanother bad line\n";
    assert!(parse_ps_output(malformed_lines).is_err());

    let mixed = "invalid line\n  42  1 /bin/bash /bin/bash -c echo\n";
    let obs = parse_ps_output(mixed).expect("valid line should be extracted");
    assert_eq!(obs.len(), 1);
    assert_eq!(obs[0].pid, Some(42));
    assert_eq!(obs[0].ppid, Some(1));
    assert_eq!(obs[0].executable, "/bin/bash");
}
