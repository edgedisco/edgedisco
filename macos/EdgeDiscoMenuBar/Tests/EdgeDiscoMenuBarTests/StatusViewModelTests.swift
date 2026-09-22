import EdgeDiscoIPC
@testable import EdgeDiscoMenuBar
import XCTest

@MainActor
final class StatusViewModelTests: XCTestCase {
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
}
