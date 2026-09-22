import Foundation
import XCTest
@testable import EdgeDiscoIPC

final class SocketPathResolverTests: XCTestCase {
    private var directory: URL!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("EdgeDiscoResolver-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    func testResolverPrefersReadableSystemSocketPath() throws {
        let system = directory.appendingPathComponent("system.sock")
        let user = directory.appendingPathComponent("user.sock")
        XCTAssertTrue(FileManager.default.createFile(atPath: system.path, contents: Data()))
        XCTAssertTrue(FileManager.default.createFile(atPath: user.path, contents: Data()))
        let resolver = SocketPathResolver(systemPath: system.path, userPath: user.path)

        XCTAssertEqual(resolver.resolve(), system.path)
    }

    func testResolverFallsBackToReadableUserSocketPath() throws {
        let system = directory.appendingPathComponent("missing-system.sock")
        let user = directory.appendingPathComponent("user.sock")
        XCTAssertTrue(FileManager.default.createFile(atPath: user.path, contents: Data()))
        let resolver = SocketPathResolver(systemPath: system.path, userPath: user.path)

        XCTAssertEqual(resolver.resolve(), user.path)
    }

    func testClientReportsDaemonNotRunningWhenNeitherPathExists() {
        let resolver = SocketPathResolver(
            systemPath: directory.appendingPathComponent("missing-system.sock").path,
            userPath: directory.appendingPathComponent("missing-user.sock").path
        )

        let state = EdgeDiscoClient(resolver: resolver).status()

        XCTAssertEqual(state, .daemonNotRunning)
    }
}
