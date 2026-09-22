import Foundation
import XCTest
@testable import EdgeDiscoIPC

final class ClientHappyPathTests: XCTestCase {
    func testStatusUsesOneNewlineTerminatedConnectionAndDecodesResult() throws {
        let server = try TestUnixServer { request in
            responseFrame(
                request: request,
                resultJSON: #"{"healthy":true,"started_at":"2026-09-22T00:00:00Z","last_scan_at":"2026-09-22T00:01:00Z","last_scan_asset_count":3,"device_count":1,"detection_count":3}"#
            )
        }
        let client = EdgeDiscoClient(socketPath: server.path)

        let state = client.status()

        guard case let .connected(status) = state else {
            return XCTFail("expected connected status, got \(state)")
        }
        XCTAssertTrue(status.healthy)
        XCTAssertEqual(status.lastScanAssetCount, 3)
        XCTAssertEqual(status.deviceCount, 1)
        XCTAssertEqual(status.detectionCount, 3)
        XCTAssertTrue(server.wait())
        let observed = server.snapshot()
        XCTAssertEqual(observed.connections, 1)
        XCTAssertEqual(observed.frame.last, 0x0A)
        XCTAssertEqual(observed.frame.filter { $0 == 0x0A }.count, 1)
        XCTAssertTrue(observed.clientClosed)
        let request = try JSONDecoder().decode(IpcRequest.self, from: observed.frame.dropLast())
        XCTAssertEqual(request.protocolVersion, 1)
        XCTAssertEqual(request.method, "status")
        XCTAssertFalse(request.requestID.isEmpty)
    }

    func testNegotiateDecodesSupportedVersion() throws {
        let server = try TestUnixServer { request in
            responseFrame(
                request: request,
                resultJSON: #"{"protocol_version":1,"supported_versions":[1]}"#
            )
        }
        let client = EdgeDiscoClient(socketPath: server.path)

        let state = client.negotiate()

        XCTAssertEqual(
            state,
            .connected(NegotiateResult(protocolVersion: 1, supportedVersions: [1]))
        )
        XCTAssertTrue(server.wait())
    }
}
