import Combine
import EdgeDiscoIPC
import Foundation

enum StatusPresentationState: Equatable {
    case healthy
    case unhealthy
    case daemonNotRunning
    case protocolError(String)
}

enum InventoryScope: String, CaseIterable, Identifiable, Sendable {
    case user = "My Session"
    case system = "This Mac"
    var id: String { rawValue }
    var socketPath: String {
        switch self {
        case .user: SocketPathResolver().userPath
        case .system: SocketPathResolver.systemSocketPath
        }
    }
}

@MainActor
final class StatusViewModel: ObservableObject {
    typealias StatusFetcher = @Sendable () async -> ConnectionState<StatusResult>
    typealias ScanFetcher = @Sendable () async -> Result<ScanResult, IpcError>
    typealias DetectionsFetcher = @Sendable () async -> Result<[SanitizedDetection], IpcError>

    @Published private(set) var state: StatusPresentationState = .daemonNotRunning
    @Published private(set) var assetCount = 0
    @Published private(set) var deviceCount = 0
    @Published private(set) var lastScanTimestamp: String?
    @Published private(set) var statusMessage: String? = "EdgeDisco daemon is not running"
    @Published private(set) var isScanning = false
    @Published private(set) var isLoadingDetections = false
    @Published private(set) var detections: [SanitizedDetection] = []
    @Published private(set) var detectionsError: String?
    @Published var scope: InventoryScope = .user {
        didSet {
            generation += 1
            detections = []
            detectionsError = nil
            resetUnavailableState(state: .daemonNotRunning, message: "Connecting to \(scope.rawValue)…")
        }
    }
    private var generation = 0

    private let statusFetcher: StatusFetcher?
    private let scanFetcher: ScanFetcher?
    private let detectionsFetcher: DetectionsFetcher?

    var isHealthy: Bool { state == .healthy }

    init(
        statusFetcher: StatusFetcher? = nil,
        scanFetcher: ScanFetcher? = nil,
        detectionsFetcher: DetectionsFetcher? = nil
    ) {
        self.statusFetcher = statusFetcher
        self.scanFetcher = scanFetcher
        self.detectionsFetcher = detectionsFetcher
    }

    func refresh() async {
        let generation = generation
        let client = EdgeDiscoClient(socketPath: scope.socketPath)
        let statusFetcher = statusFetcher ?? { await client.status() }
        let connectionState = await statusFetcher()
        guard generation == self.generation else { return }
        apply(connectionState)
    }

    func scanNow() async {
        guard !isScanning else { return }
        isScanning = true
        defer { isScanning = false }

        let generation = generation
        let client = EdgeDiscoClient(socketPath: scope.socketPath)
        let scanFetcher = scanFetcher ?? { await client.scan() }
        let result = await scanFetcher()
        guard generation == self.generation else { return }
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

        let generation = generation
        let client = EdgeDiscoClient(socketPath: scope.socketPath)
        let detectionsFetcher = detectionsFetcher ?? { await client.detections() }
        let result = await detectionsFetcher()
        guard generation == self.generation else { return }
        switch result {
        case let .success(detections):
            self.detections = detections
            detectionsError = nil
        case .failure(.daemonNotRunning):
            statusMessage = "Could not load detections: EdgeDisco daemon is not running"
            detectionsError = statusMessage
        case let .failure(.protocolError(message)):
            statusMessage = "Could not load detections: \(message)"
            detectionsError = statusMessage
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
