use edgedisco_cli::commands::daemon::run_daemon_iteration;
use edgedisco_cli::commands::status::format_status;
use edgedisco_core::store::Store;
use tempfile::NamedTempFile;

#[test]
fn test_daemon_iteration_and_status_reporting() {
    let tmp = NamedTempFile::new().expect("create temp file");
    let db_path = tmp.path();

    let store = Store::open(db_path).expect("open store");

    let count = run_daemon_iteration(&store).expect("run daemon iteration");
    assert!(count < usize::MAX);

    // Check device enrolled
    let devices = store.list_devices(5).expect("list devices");
    assert_eq!(devices.len(), 1);

    // Format status
    let status = format_status(&store, db_path, false).expect("format status");
    assert!(status.contains("Hostname"));
    assert!(status.contains("Assets"));
}
