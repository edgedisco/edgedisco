use edgedisco_cli::commands::scan::generate_scan_report;
use edgedisco_core::models::ScanReport;
use edgedisco_core::redaction::validate_report;

#[test]
fn test_generate_scan_report_schema_compliance() {
    let report = generate_scan_report().expect("generate scan report");

    assert!(!report.scan_id.is_empty());
    assert!(!report.observed_at.is_empty());
    assert!(!report.device.hostname.is_empty());
    assert!(!report.device.os.is_empty());

    // Validate report against core domain rules
    validate_report(&report).expect("validate scan report schema");

    // Serialize to JSON and deserialize back
    let json_bytes = serde_json::to_vec(&report).expect("serialize report");
    let deserialized: ScanReport =
        serde_json::from_slice(&json_bytes).expect("deserialize scan report");

    assert_eq!(deserialized.scan_id, report.scan_id);
    assert_eq!(deserialized.assets.len(), report.assets.len());
}
