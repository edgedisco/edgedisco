import Foundation
import XCTest
@testable import EdgeDiscoIPC

final class ClientFailureTests: XCTestCase {
    func testNegotiateRejectsUnsupportedSelectedVersion() throws {
        let server = try TestUnixServer { request in
            responseFrame(
                request: request,
                resultJSON: #"{"protocol_version":2,"supported_versions":[2]}"#
            )
        }

        let state = EdgeDiscoClient(socketPath: server.path).negotiate()

        guard case let .protocolError(message) = state else {
            return XCTFail("expected protocol error, got \(state)")
        }
        XCTAssertTrue(message.contains("does not support protocol version 1"))
        XCTAssertTrue(server.wait())
    }

    func testOversizedResponseReturnsBoundedError() throws {
        let server = try TestUnixServer { _ in
            var data = Data(repeating: 0x61, count: EdgeDiscoClient.maximumResponseBytes + 1)
            data.append(0x0A)
            return data
        }

        let state = EdgeDiscoClient(socketPath: server.path).status()

        assertProtocolError(state, contains: "exceeds")
        XCTAssertTrue(server.wait())
    }

    func testPartialJSONReturnsBoundedError() throws {
        let server = try TestUnixServer { _ in Data("{\n".utf8) }

        let state = EdgeDiscoClient(socketPath: server.path).status()

        assertProtocolError(state, contains: "invalid response")
        XCTAssertTrue(server.wait())
    }

    func testMissingNewlineReturnsBoundedError() throws {
        let server = try TestUnixServer { request in
            var frame = responseFrame(
                request: request,
                resultJSON: #"{"healthy":true,"started_at":"now","last_scan_at":null,"last_scan_asset_count":null,"device_count":0,"detection_count":0}"#
            )
            frame.removeLast()
            return frame
        }

        let state = EdgeDiscoClient(socketPath: server.path).status()

        assertProtocolError(state, contains: "newline")
        XCTAssertTrue(server.wait())
    }

    func testSilentServerTimesOutInUnderThreeSeconds() throws {
        let server = try TestUnixServer(holdOpen: 3) { _ in nil }
        let started = Date()

        let state = EdgeDiscoClient(socketPath: server.path).status()
        let elapsed = Date().timeIntervalSince(started)

        assertProtocolError(state, contains: "timed out")
        XCTAssertGreaterThan(elapsed, 1.5)
        XCTAssertLessThan(elapsed, 3)
    }

    private func assertProtocolError<Value>(
        _ state: ConnectionState<Value>,
        contains expectedText: String,
        file: StaticString = #filePath,
        line: UInt = #line
    ) where Value: Equatable & Sendable {
        guard case let .protocolError(message) = state else {
            return XCTFail("expected protocol error, got \(state)", file: file, line: line)
        }
        XCTAssertTrue(message.contains(expectedText), "unexpected message: \(message)", file: file, line: line)
    }
}
