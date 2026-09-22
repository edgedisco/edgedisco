import Darwin
import Foundation

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

    private func exchange(frame: Data, at path: String, timeout requestTimeout: TimeInterval) async throws -> Data {
        return try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                continuation.resume(with: Result {
                    try Self.exchangeBlocking(frame: frame, at: path, timeout: requestTimeout)
                })
            }
        }
    }

    private static func exchangeBlocking(frame: Data, at path: String, timeout: TimeInterval) throws -> Data {
        var address = sockaddr_un()
        let pathBytes = Array(path.utf8) + [0]
        guard pathBytes.count <= MemoryLayout.size(ofValue: address.sun_path) else {
            throw TransportFailure.message("Unix socket path is too long")
        }
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        address.sun_family = sa_family_t(AF_UNIX)
        withUnsafeMutableBytes(of: &address.sun_path) { $0.copyBytes(from: pathBytes) }

        let socket = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard socket >= 0 else { throw TransportFailure.message("could not create Unix socket") }
        defer { Darwin.close(socket) }
        var noSigPipe: Int32 = 1
        _ = setsockopt(socket, SOL_SOCKET, SO_NOSIGPIPE, &noSigPipe, socklen_t(MemoryLayout.size(ofValue: noSigPipe)))
        let flags = fcntl(socket, F_GETFL)
        guard flags >= 0, fcntl(socket, F_SETFL, flags | O_NONBLOCK) == 0 else {
            throw TransportFailure.message("could not configure Unix socket")
        }

        let deadline = ProcessInfo.processInfo.systemUptime + timeout
        let connected = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(socket, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        if connected != 0 {
            let code = errno
            if code == EINPROGRESS {
                try waitFor(socket, events: Int16(POLLOUT), deadline: deadline)
                var socketError: Int32 = 0
                var length = socklen_t(MemoryLayout.size(ofValue: socketError))
                guard getsockopt(socket, SOL_SOCKET, SO_ERROR, &socketError, &length) == 0 else {
                    throw TransportFailure.message("could not check daemon connection")
                }
                if socketError != 0 { try throwConnectionError(socketError) }
            } else {
                try throwConnectionError(code)
            }
        }

        try frame.withUnsafeBytes { bytes in
            guard let start = bytes.baseAddress else { return }
            var sent = 0
            while sent < bytes.count {
                guard ProcessInfo.processInfo.systemUptime < deadline else {
                    throw TransportFailure.message("request timed out")
                }
                let count = Darwin.send(socket, start.advanced(by: sent), bytes.count - sent, 0)
                if count > 0 { sent += count; continue }
                if count < 0 && errno == EINTR { continue }
                if count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) {
                    try waitFor(socket, events: Int16(POLLOUT), deadline: deadline)
                    continue
                }
                throw TransportFailure.message("could not write request: \(posixMessage(errno))")
            }
        }

        var response = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while true {
            guard ProcessInfo.processInfo.systemUptime < deadline else {
                throw TransportFailure.message("request timed out")
            }
            let count = buffer.withUnsafeMutableBytes { Darwin.recv(socket, $0.baseAddress, $0.count, 0) }
            if count > 0 {
                response.append(contentsOf: buffer[..<count])
                guard response.count <= maximumResponseBytes else {
                    throw TransportFailure.message("response exceeds \(maximumResponseBytes)-byte limit")
                }
                if let newline = response.firstIndex(of: 0x0A) { return response[..<newline] }
                continue
            }
            if count == 0 { throw TransportFailure.message("response ended before newline terminator") }
            if errno == EINTR { continue }
            if errno == EAGAIN || errno == EWOULDBLOCK {
                try waitFor(socket, events: Int16(POLLIN), deadline: deadline)
                continue
            }
            throw TransportFailure.message("could not read response: \(posixMessage(errno))")
        }
    }

    private static func waitFor(_ socket: Int32, events: Int16, deadline: TimeInterval) throws {
        var descriptor = pollfd(fd: socket, events: events, revents: 0)
        while true {
            let remaining = deadline - ProcessInfo.processInfo.systemUptime
            guard remaining > 0 else { throw TransportFailure.message("request timed out") }
            let milliseconds = Int32(min(Double(Int32.max), max(1, remaining * 1000)))
            let result = Darwin.poll(&descriptor, 1, milliseconds)
            if result > 0 { return }
            if result == 0 { throw TransportFailure.message("request timed out") }
            if errno != EINTR { throw TransportFailure.message("socket wait failed: \(posixMessage(errno))") }
        }
    }

    private static func throwConnectionError(_ code: Int32) throws -> Never {
        if code == ENOENT || code == ECONNREFUSED { throw TransportFailure.daemonNotRunning }
        throw TransportFailure.message("could not connect to daemon: \(posixMessage(code))")
    }

    private static func posixMessage(_ code: Int32) -> String {
        POSIXError(POSIXErrorCode(rawValue: code) ?? .EIO).localizedDescription
    }
}
