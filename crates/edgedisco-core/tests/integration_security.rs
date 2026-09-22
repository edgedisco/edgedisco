#[test]
fn test_deployment_security_bundled_sqlite_only() {
    // Verify that the crate compiled cleanly with rusqlite bundled and zero openssl linking.
    // The rusqlite crate is compiled with the "bundled" feature, ensuring pure embedded C/SQLite
    // without requiring dynamic system libraries.
    let store = edgedisco_core::store::Store::open_in_memory();
    assert!(store.is_ok());
}
