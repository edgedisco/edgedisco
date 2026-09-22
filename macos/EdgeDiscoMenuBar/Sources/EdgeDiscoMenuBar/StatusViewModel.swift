import Combine
import EdgeDiscoIPC
import Foundation

enum StatusPresentationState: Equatable {
    case healthy
    case unhealthy
    case daemonNotRunning
    case protocolError(String)
}

@MainActor
final class StatusViewModel: ObservableObject {
    typealias StatusFetcher = @Sendable () -> ConnectionState<StatusResult>

    @Published private(set) var state: StatusPresentationState = .daemonNotRunning
    @Published private(set) var assetCount = 0
    @Published private(set) var deviceCount = 0
    @Published private(set) var lastScanTimestamp: String?
    @Published private(set) var statusMessage: String? = "EdgeDisco daemon is not running"

    private let statusFetcher: StatusFetcher

    var isHealthy: Bool { state == .healthy }

    init(statusFetcher: @escaping StatusFetcher = { EdgeDiscoClient().status() }) {
        self.statusFetcher = statusFetcher
    }

    func refresh() async {
        let statusFetcher = statusFetcher
        let connectionState = await Task.detached(priority: .utility) {
            statusFetcher()
        }.value
        apply(connectionState)
    }

    func apply(_ connectionState: ConnectionState<StatusResult>) {
        switch connectionState {
        case let .connected(status):
            state = status.healthy ? .healthy : .unhealthy
            assetCount = status.lastScanAssetCount ?? 0
            deviceCount = status.deviceCount
            lastScanTimestamp = status.lastScanAt
            statusMessage = status.healthy ? nil : "EdgeDisco daemon reported an unhealthy state"
        case .daemonNotRunning:
            resetUnavailableState(
                state: .daemonNotRunning,
                message: "EdgeDisco daemon is not running"
            )
        case let .protocolError(message):
            resetUnavailableState(
                state: .protocolError(message),
                message: "EdgeDisco protocol error: \(message)"
            )
        }
    }

    private func resetUnavailableState(state: StatusPresentationState, message: String) {
        self.state = state
        assetCount = 0
        deviceCount = 0
        lastScanTimestamp = nil
        statusMessage = message
    }
}
