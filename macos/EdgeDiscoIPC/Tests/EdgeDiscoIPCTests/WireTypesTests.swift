import XCTest
@testable import EdgeDiscoIPC

final class WireTypesTests: XCTestCase {
    func testRequestEncodesDocumentedSnakeCaseEnvelope() throws {
        let request = IpcRequest(protocolVersion: 1, requestID: "request-1", method: "status")

        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as? [String: Any]
        )

        XCTAssertEqual(object["protocol_version"] as? Int, 1)
        XCTAssertEqual(object["request_id"] as? String, "request-1")
        XCTAssertEqual(object["method"] as? String, "status")
        XCTAssertEqual(object.count, 3)
    }

    func testStatusResultDecodesDocumentedFields() throws {
        let json = Data(#"{"healthy":true,"started_at":"2026-09-22T00:00:00Z","last_scan_at":"2026-09-22T00:01:00Z","last_scan_asset_count":3,"device_count":1,"detection_count":3}"#.utf8)

        let status = try JSONDecoder().decode(StatusResult.self, from: json)

        XCTAssertTrue(status.healthy)
        XCTAssertEqual(status.startedAt, "2026-09-22T00:00:00Z")
        XCTAssertEqual(status.lastScanAt, "2026-09-22T00:01:00Z")
        XCTAssertEqual(status.lastScanAssetCount, 3)
        XCTAssertEqual(status.deviceCount, 1)
        XCTAssertEqual(status.detectionCount, 3)
    }

    func testResponseDecodesProtocolError() throws {
        let json = Data(#"{"protocol_version":1,"request_id":"request-1","ok":false,"error":{"code":"unknown_method","message":"not exposed"}}"#.utf8)

        let response = try JSONDecoder().decode(IpcResponse<StatusResult>.self, from: json)

        XCTAssertFalse(response.ok)
        XCTAssertEqual(response.error, ProtocolError(code: "unknown_method", message: "not exposed"))
        XCTAssertNil(response.result)
    }
}
