import EdgeDiscoIPC
@testable import EdgeDiscoMenuBar
import XCTest

@MainActor
final class StatusViewModelTests: XCTestCase {
    func testScopesUseDistinctExplicitSockets() {
        XCTAssertEqual(InventoryScope.system.socketPath, SocketPathResolver.systemSocketPath)
        XCTAssertEqual(InventoryScope.user.socketPath, SocketPathResolver().userPath)
        XCTAssertNotEqual(InventoryScope.user.socketPath, InventoryScope.system.socketPath)
        XCTAssertEqual(StatusViewModel().scope, .user)
    }

    func testLateResponseFromPreviousScopeCannotPopulateNewScope() async {
        let started = expectation(description: "old scope request started")
        let release = AsyncGate()
        let viewModel = StatusViewModel(statusFetcher: {
            started.fulfill()
            await release.wait()
            return .connected(StatusResult(healthy: true, startedAt: "now", lastScanAt: nil,
                lastScanAssetCount: 99, deviceCount: 1, detectionCount: 99))
        })
        let task = Task { await viewModel.refresh() }
        await fulfillment(of: [started], timeout: 1)
        viewModel.scope = .system
        await release.open()
        await task.value
        XCTAssertEqual(viewModel.assetCount, 0)
        XCTAssertFalse(viewModel.isHealthy)
    }

    func testDetectionFailureRemainsVisibleAfterHealthyStatusRefresh() async {
        let viewModel = StatusViewModel(detectionsFetcher: { .failure(.daemonNotRunning) })
        await viewModel.loadDetections()
        viewModel.apply(.connected(StatusResult(healthy: true, startedAt: "now", lastScanAt: nil,
            lastScanAssetCount: 1, deviceCount: 1, detectionCount: 1)))
        XCTAssertNotNil(viewModel.detectionsError)
        viewModel.scope = .system
        XCTAssertNil(viewModel.detectionsError)
        XCTAssertTrue(viewModel.detections.isEmpty)
    }

    func testStateTransitionsFromHealthyToDaemonNotRunningToProtocolError() {
        let viewModel = StatusViewModel()
        let healthyStatus = StatusResult(
            healthy: true,
            startedAt: "2026-09-22T09:00:00Z",
            lastScanAt: "2026-09-22T10:00:00Z",
            lastScanAssetCount: 7,
            deviceCount: 3,
            detectionCount: 5
        )

        viewModel.apply(.connected(healthyStatus))

        XCTAssertEqual(viewModel.state, .healthy)
        XCTAssertTrue(viewModel.isHealthy)
        XCTAssertEqual(viewModel.assetCount, 7)
        XCTAssertEqual(viewModel.deviceCount, 3)
        XCTAssertEqual(viewModel.lastScanTimestamp, "2026-09-22T10:00:00Z")
        XCTAssertNil(viewModel.statusMessage)

        viewModel.apply(.daemonNotRunning)

        XCTAssertEqual(viewModel.state, .daemonNotRunning)
        XCTAssertFalse(viewModel.isHealthy)
        XCTAssertEqual(viewModel.assetCount, 0)
        XCTAssertEqual(viewModel.deviceCount, 0)
        XCTAssertNil(viewModel.lastScanTimestamp)
        XCTAssertEqual(viewModel.statusMessage, "EdgeDisco daemon is not running")

        viewModel.apply(.protocolError("unsupported response"))

        XCTAssertEqual(viewModel.state, .protocolError("unsupported response"))
        XCTAssertFalse(viewModel.isHealthy)
        XCTAssertEqual(viewModel.assetCount, 0)
        XCTAssertEqual(viewModel.deviceCount, 0)
        XCTAssertEqual(viewModel.statusMessage, "EdgeDisco protocol error: unsupported response")
    }

    func testRefreshFetchesAndAppliesStatus() async {
        let status = StatusResult(
            healthy: true,
            startedAt: "2026-09-22T09:00:00Z",
            lastScanAt: "2026-09-22T10:00:00Z",
            lastScanAssetCount: 9,
            deviceCount: 4,
            detectionCount: 6
        )
        let viewModel = StatusViewModel(statusFetcher: { .connected(status) })

        await viewModel.refresh()

        XCTAssertEqual(viewModel.state, .healthy)
        XCTAssertEqual(viewModel.assetCount, 9)
        XCTAssertEqual(viewModel.deviceCount, 4)
    }

    func testUnhealthyConnectedStatusUsesWarningStateAndCounts() {
        let viewModel = StatusViewModel()
        let status = StatusResult(
            healthy: false,
            startedAt: "2026-09-22T09:00:00Z",
            lastScanAt: nil,
            lastScanAssetCount: nil,
            deviceCount: 2,
            detectionCount: 4
        )

        viewModel.apply(.connected(status))

        XCTAssertEqual(viewModel.state, .unhealthy)
        XCTAssertFalse(viewModel.isHealthy)
        XCTAssertEqual(viewModel.assetCount, 0)
        XCTAssertEqual(viewModel.deviceCount, 2)
        XCTAssertEqual(viewModel.statusMessage, "EdgeDisco daemon reported an unhealthy state")
    }

    func testScanNowIsInFlightUntilFetcherCompletesAndUpdatesAssetCount() async {
        let started = expectation(description: "scan dispatched")
        let release = AsyncGate()
        let viewModel = StatusViewModel(scanFetcher: {
            started.fulfill()
            await release.wait()
            return .success(ScanResult(accepted: true, assetCount: 14))
        })

        let task = Task { await viewModel.scanNow() }
        await fulfillment(of: [started], timeout: 1)

        XCTAssertTrue(viewModel.isScanning)

        await release.open()
        await task.value

        XCTAssertFalse(viewModel.isScanning)
        XCTAssertEqual(viewModel.assetCount, 14)
    }

    func testLoadDetectionsPublishesDecodedList() async {
        let decoded = try! JSONDecoder().decode(
            [SanitizedDetection].self,
            from: Data(#"[{"kind":"cli","name":"Ollama","vendor":"Ollama","version":null,"running":true,"present":true,"last_seen":"2026-09-22T10:00:00Z","extra":"ignored"}]"#.utf8)
        )
        let viewModel = StatusViewModel(detectionsFetcher: { .success(decoded) })

        await viewModel.loadDetections()

        XCTAssertEqual(viewModel.detections, decoded)
        XCTAssertEqual(viewModel.detections.first?.name, "Ollama")
        XCTAssertEqual(viewModel.detections.first?.running, true)
    }

    func testScanFailureRestoresIdleStateAndPublishesError() async {
        let viewModel = StatusViewModel(
            scanFetcher: { .failure(.protocolError("connection reset by peer")) }
        )

        await viewModel.scanNow()

        XCTAssertFalse(viewModel.isScanning)
        XCTAssertEqual(viewModel.statusMessage, "Scan failed: connection reset by peer")
    }

    func testDetectionRunningLabelDescribesBothStates() {
        let running = SanitizedDetection(
            kind: "desktop_app",
            name: "Claude",
            vendor: "Anthropic",
            version: nil,
            running: true,
            present: true,
            lastSeen: nil
        )
        let stopped = SanitizedDetection(
            kind: "cli",
            name: "Ollama",
            vendor: "Ollama",
            version: nil,
            running: false,
            present: true,
            lastSeen: nil
        )

        XCTAssertEqual(running.runningLabel, "Running")
        XCTAssertEqual(stopped.runningLabel, "Not running")
    }
}

private actor AsyncGate {
    private var isOpen = false
    private var continuation: CheckedContinuation<Void, Never>?

    func wait() async {
        guard !isOpen else { return }
        await withCheckedContinuation { continuation = $0 }
    }

    func open() {
        isOpen = true
        continuation?.resume()
        continuation = nil
    }
}
