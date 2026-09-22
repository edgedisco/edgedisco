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
    typealias ScanFetcher = @Sendable () -> Result<ScanResult, IpcError>
    typealias DetectionsFetcher = @Sendable () -> Result<[SanitizedDetection], IpcError>

    @Published private(set) var state: StatusPresentationState = .daemonNotRunning
    @Published private(set) var assetCount = 0
    @Published private(set) var deviceCount = 0
    @Published private(set) var lastScanTimestamp: String?
    @Published private(set) var statusMessage: String? = "EdgeDisco daemon is not running"
    @Published private(set) var isScanning = false
    @Published private(set) var isLoadingDetections = false
    @Published private(set) var detections: [SanitizedDetection] = []

    private let statusFetcher: StatusFetcher
    private let scanFetcher: ScanFetcher
    private let detectionsFetcher: DetectionsFetcher

    var isHealthy: Bool { state == .healthy }

    init(
        statusFetcher: @escaping StatusFetcher = { EdgeDiscoClient().status() },
        scanFetcher: @escaping ScanFetcher = { EdgeDiscoClient().scan() },
        detectionsFetcher: @escaping DetectionsFetcher = { EdgeDiscoClient().detections() }
    ) {
        self.statusFetcher = statusFetcher
        self.scanFetcher = scanFetcher
        self.detectionsFetcher = detectionsFetcher
    }

    func refresh() async {
        let statusFetcher = statusFetcher
        let connectionState = await Task.detached(priority: .utility) {
            statusFetcher()
        }.value
        apply(connectionState)
    }

    func scanNow() async {
        guard !isScanning else { return }
        isScanning = true
        defer { isScanning = false }

        let scanFetcher = scanFetcher
        let result = await Task.detached(priority: .utility) {
            scanFetcher()
        }.value
        switch result {
        case let .success(scan):
            assetCount = Int(clamping: scan.assetCount)
            statusMessage = nil
        case .failure(.daemonNotRunning):
            statusMessage = "Scan failed: EdgeDisco daemon is not running"
        case let .failure(.protocolError(message)):
            statusMessage = "Scan failed: \(message)"
        }
    }

    func loadDetections() async {
        guard !isLoadingDetections else { return }
        isLoadingDetections = true
        defer { isLoadingDetections = false }

        let detectionsFetcher = detectionsFetcher
        let result = await Task.detached(priority: .utility) {
            detectionsFetcher()
        }.value
        switch result {
        case let .success(detections):
            self.detections = detections
        case .failure(.daemonNotRunning):
            statusMessage = "Could not load detections: EdgeDisco daemon is not running"
        case let .failure(.protocolError(message)):
            statusMessage = "Could not load detections: \(message)"
        }
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
