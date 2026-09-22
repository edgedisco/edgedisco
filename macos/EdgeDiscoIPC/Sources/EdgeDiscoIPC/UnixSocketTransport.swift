import Darwin
import Foundation
import Network

public enum ConnectionState<Value: Equatable & Sendable>: Equatable, Sendable {
    case connected(Value)
    case daemonNotRunning
    case protocolError(String)
}

public enum IpcError: Error, Equatable, Sendable {
    case daemonNotRunning
    case protocolError(String)
}

public struct SocketPathResolver: Sendable {
    public static let systemSocketPath = "/var/run/edgedisco.sock"

    public let systemPath: String
    public let userPath: String

    public init(
        systemPath: String = SocketPathResolver.systemSocketPath,
        userPath: String = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".edgedisco/edgedisco.sock").path
    ) {
        self.systemPath = systemPath
        self.userPath = userPath
    }

    public func resolve() -> String? {
        if FileManager.default.isReadableFile(atPath: systemPath) {
            return systemPath
        }
        if FileManager.default.isReadableFile(atPath: userPath) {
            return userPath
        }
        return nil
    }
}

public struct EdgeDiscoClient: Sendable {
    public static let maximumRequestBytes = 16 * 1024
    public static let maximumResponseBytes = 256 * 1024
    public static let timeout: TimeInterval = 2
    public static let explicitScanTimeout: TimeInterval = 120
    public static let connectionTestTimeout: TimeInterval = 12

    private enum PathSource: Sendable {
        case fixed(String)
        case resolved(SocketPathResolver)

        func path() -> String? {
            switch self {
            case let .fixed(path): path
            case let .resolved(resolver): resolver.resolve()
            }
        }
    }

    private enum TransportFailure: Error {
        case daemonNotRunning
        case message(String)
    }

    private let pathSource: PathSource

    public init(socketPath: String) {
        pathSource = .fixed(socketPath)
    }

    public init(resolver: SocketPathResolver = SocketPathResolver()) {
        pathSource = .resolved(resolver)
    }

    public func negotiate() async -> ConnectionState<NegotiateResult> {
        let state: ConnectionState<NegotiateResult> = await perform(method: "negotiate")
        guard case let .connected(result) = state else { return state }
        guard result.protocolVersion == edgeDiscoProtocolVersion,
              result.supportedVersions.contains(edgeDiscoProtocolVersion)
        else {
            return .protocolError("daemon does not support protocol version \(edgeDiscoProtocolVersion)")
        }
        return state
    }

    public func status() async -> ConnectionState<StatusResult> {
        await perform(method: "status")
    }

    public func settings() async -> ConnectionState<SettingsSnapshot> {
        await perform(method: "settings_get")
    }

    public func exportDiagnostics() async -> ConnectionState<ExportDiagnostics> {
        await perform(method: "export_diagnostics")
    }

    public func testOTLPConnection() async -> ConnectionState<ConnectionTestResult> {
        await perform(method: "otlp_test_connection", timeout: Self.connectionTestTimeout)
    }

    public func applySettings(_ settings: DaemonSettings, expectedRevision: String) async -> ConnectionState<SettingsSnapshot> {
        await perform(method: "settings_set", payload: SettingsUpdateRequest(expectedRevision: expectedRevision, settings: settings))
    }

    public func scan() async -> Result<ScanResult, IpcError> {
        result(from: await perform(method: "scan", timeout: Self.explicitScanTimeout))
    }

    public func detections() async -> Result<[SanitizedDetection], IpcError> {
        let state: ConnectionState<DetectionsResult> = await perform(method: "detections")
        switch state {
        case let .connected(result):
            return .success(result.detections)
        case .daemonNotRunning:
            return .failure(.daemonNotRunning)
        case let .protocolError(message):
            return .failure(.protocolError(message))
        }
    }

    private func result<Value>(from state: ConnectionState<Value>) -> Result<Value, IpcError> {
        switch state {
        case let .connected(value): .success(value)
        case .daemonNotRunning: .failure(.daemonNotRunning)
        case let .protocolError(message): .failure(.protocolError(message))
        }
    }

    private func perform<Result: Codable & Equatable & Sendable>(
        method: String,
        payload: SettingsUpdateRequest? = nil,
        timeout: TimeInterval = Self.timeout
    ) async -> ConnectionState<Result> {
        guard let socketPath = pathSource.path() else { return .daemonNotRunning }
        let requestID = UUID().uuidString
        let request = IpcRequest(
            protocolVersion: edgeDiscoProtocolVersion,
            requestID: requestID,
            method: method,
            payload: payload
        )

        do {
            var frame = try JSONEncoder().encode(request)
            frame.append(0x0A)
            guard frame.count <= Self.maximumRequestBytes else {
                return .protocolError("request exceeds \(Self.maximumRequestBytes)-byte limit")
            }
            let responseData = try await exchange(frame: frame, at: socketPath, timeout: timeout)
            let response = try JSONDecoder().decode(IpcResponse<Result>.self, from: responseData)
            guard response.protocolVersion == edgeDiscoProtocolVersion else {
                return .protocolError("unsupported response protocol version \(response.protocolVersion)")
            }
            guard response.requestID == requestID else {
                return .protocolError("response request_id does not match request")
            }
            guard response.ok else {
                let error = response.error
                return .protocolError(error.map { "\($0.code): \($0.message)" } ?? "daemon returned an unspecified protocol error")
            }
            guard response.error == nil, let result = response.result else {
                return .protocolError("successful response is missing result or contains error")
            }
            return .connected(result)
        } catch TransportFailure.daemonNotRunning {
            return .daemonNotRunning
        } catch let TransportFailure.message(message) {
            return .protocolError(message)
        } catch {
            return .protocolError("invalid response: \(error.localizedDescription)")
        }
    }

    private final class ExchangeState: @unchecked Sendable {
        private let lock = NSLock()
        private var isResumed = false
        func markResumed() -> Bool {
            lock.lock()
            defer { lock.unlock() }
            let old = isResumed
            isResumed = true
            return !old
        }
    }

    private func exchange(frame: Data, at path: String, timeout requestTimeout: TimeInterval) async throws -> Data {
        let address = sockaddr_un()
        guard path.utf8.count < MemoryLayout.size(ofValue: address.sun_path) else {
            throw TransportFailure.message("Unix socket path is too long")
        }
        let connection = NWConnection(to: .unix(path: path), using: .tcp)
        let state = ExchangeState()

        return try await withCheckedThrowingContinuation { continuation in
            let timeoutTask = Task {
                try await Task.sleep(nanoseconds: UInt64(requestTimeout * 1_000_000_000))
                if state.markResumed() {
                    connection.cancel()
                    continuation.resume(throwing: TransportFailure.message("request timed out"))
                }
            }

            connection.stateUpdateHandler = { nwState in
                switch nwState {
                case .ready:
                    connection.send(content: frame, completion: .contentProcessed({ error in
                        if let error = error {
                            if state.markResumed() {
                                timeoutTask.cancel()
                                connection.cancel()
                                continuation.resume(throwing: TransportFailure.message("could not write request: \(error.localizedDescription)"))
                            }
                            return
                        }

                        var response = Data()

                        func receiveNext() {
                            connection.receive(minimumIncompleteLength: 1, maximumLength: 4096) { data, _, isComplete, error in
                                if let data = data {
                                    response.append(data)

                                    if response.count > Self.maximumResponseBytes {
                                        if state.markResumed() {
                                            timeoutTask.cancel()
                                            connection.cancel()
                                            continuation.resume(throwing: TransportFailure.message("response exceeds \(Self.maximumResponseBytes)-byte limit"))
                                        }
                                        return
                                    }

                                    if let newlineIndex = response.firstIndex(of: 0x0A) {
                                        if state.markResumed() {
                                            timeoutTask.cancel()
                                            connection.cancel()
                                            continuation.resume(returning: response[..<newlineIndex])
                                        }
                                        return
                                    }
                                }

                                if let error = error {
                                    if state.markResumed() {
                                        timeoutTask.cancel()
                                        connection.cancel()
                                        continuation.resume(throwing: TransportFailure.message("could not read response: \(error.localizedDescription)"))
                                    }
                                    return
                                }

                                if isComplete {
                                    if state.markResumed() {
                                        timeoutTask.cancel()
                                        connection.cancel()
                                        continuation.resume(throwing: TransportFailure.message("response ended before newline terminator"))
                                    }
                                    return
                                }

                                receiveNext()
                            }
                        }

                        receiveNext()
                    }))
                case .failed(let error), .waiting(let error):
                    if state.markResumed() {
                        timeoutTask.cancel()
                        connection.cancel()
                        if error == NWError.posix(.ENOENT) || error == NWError.posix(.ECONNREFUSED) {
                            continuation.resume(throwing: TransportFailure.daemonNotRunning)
                        } else {
                            continuation.resume(throwing: TransportFailure.message("could not connect to daemon: \(error.localizedDescription)"))
                        }
                    }
                case .cancelled:
                    if state.markResumed() {
                        timeoutTask.cancel()
                        continuation.resume(throwing: TransportFailure.message("connection cancelled"))
                    }
                default:
                    break
                }
            }

            connection.start(queue: .global())
        }
    }
}
