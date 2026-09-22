import Foundation
import XCTest
@testable import EdgeDiscoIPC

final class ClientFailureTests: XCTestCase {
    func testOverlongSocketPathsReturnAnErrorInsteadOfCrashing() async {
        for path in [String(repeating: "x", count: 104), "/tmp/" + String(repeating: "x", count: 200), "/tmp/" + String(repeating: "é", count: 50)] {
            let state = await EdgeDiscoClient(socketPath: path).status()
            assertProtocolError(state, contains: "socket path is too long")
        }
    }

    func testNegotiateRejectsUnsupportedSelectedVersion() async throws {
        let server = try TestUnixServer { request in
            responseFrame(
                request: request,
                resultJSON: #"{"protocol_version":2,"supported_versions":[2]}"#
            )
        }

        let state = await EdgeDiscoClient(socketPath: server.path).negotiate()

        guard case let .protocolError(message) = state else {
            return XCTFail("expected protocol error, got \(state)")
        }
        XCTAssertTrue(message.contains("does not support protocol version 1"))
        XCTAssertTrue(server.wait())
    }

    func testOversizedResponseReturnsBoundedError() async throws {
        let server = try TestUnixServer { _ in
            var data = Data(repeating: 0x61, count: EdgeDiscoClient.maximumResponseBytes + 1)
            data.append(0x0A)
            return data
        }

        let state = await EdgeDiscoClient(socketPath: server.path).status()

        assertProtocolError(state, contains: "exceeds")
        XCTAssertTrue(server.wait())
    }

    func testPartialJSONReturnsBoundedError() async throws {
        let server = try TestUnixServer { _ in Data("{\n".utf8) }

        let state = await EdgeDiscoClient(socketPath: server.path).status()

        assertProtocolError(state, contains: "invalid response")
        XCTAssertTrue(server.wait())
    }

    func testMissingNewlineReturnsBoundedError() async throws {
        let server = try TestUnixServer { request in
            var frame = responseFrame(
                request: request,
                resultJSON: #"{"healthy":true,"started_at":"now","last_scan_at":null,"last_scan_asset_count":null,"device_count":0,"detection_count":0}"#
            )
            frame.removeLast()
            return frame
        }

        let state = await EdgeDiscoClient(socketPath: server.path).status()

        assertProtocolError(state, contains: "newline")
        XCTAssertTrue(server.wait())
    }

    func testSilentServerTimesOutInUnderThreeSeconds() async throws {
        let server = try TestUnixServer(holdOpen: 3) { _ in nil }
        let started = Date()

        let state = await EdgeDiscoClient(socketPath: server.path).status()
        let elapsed = Date().timeIntervalSince(started)

        assertProtocolError(state, contains: "timed out")
        XCTAssertGreaterThan(elapsed, 1.5)
        XCTAssertLessThan(elapsed, 3)
    }

    func testScanDisconnectReturnsProtocolError() async throws {
        let server = try TestUnixServer { _ in nil }

        let result = await EdgeDiscoClient(socketPath: server.path).scan()

        guard case let .failure(.protocolError(message)) = result else {
            return XCTFail("expected protocol error, got \(result)")
        }
        XCTAssertTrue(message.contains("response ended before newline"))
        XCTAssertTrue(server.wait())
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
