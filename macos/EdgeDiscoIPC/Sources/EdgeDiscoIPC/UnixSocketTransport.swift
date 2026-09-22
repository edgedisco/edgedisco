import Darwin
import Foundation

public enum ConnectionState<Value: Equatable & Sendable>: Equatable, Sendable {
    case connected(Value)
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

    public func negotiate() -> ConnectionState<NegotiateResult> {
        let state: ConnectionState<NegotiateResult> = perform(method: "negotiate")
        guard case let .connected(result) = state else { return state }
        guard result.protocolVersion == edgeDiscoProtocolVersion,
              result.supportedVersions.contains(edgeDiscoProtocolVersion)
        else {
            return .protocolError("daemon does not support protocol version \(edgeDiscoProtocolVersion)")
        }
        return state
    }

    public func status() -> ConnectionState<StatusResult> {
        perform(method: "status")
    }

    private func perform<Result: Codable & Equatable & Sendable>(
        method: String
    ) -> ConnectionState<Result> {
        guard let socketPath = pathSource.path() else { return .daemonNotRunning }
        let requestID = UUID().uuidString
        let request = IpcRequest(
            protocolVersion: edgeDiscoProtocolVersion,
            requestID: requestID,
            method: method
        )

        do {
            var frame = try JSONEncoder().encode(request)
            frame.append(0x0A)
            guard frame.count <= Self.maximumRequestBytes else {
                return .protocolError("request exceeds \(Self.maximumRequestBytes)-byte limit")
            }
            let responseData = try exchange(frame: frame, at: socketPath)
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

    private func exchange(frame: Data, at path: String) throws -> Data {
        let descriptor = socket(AF_UNIX, SOCK_STREAM, 0)
        guard descriptor >= 0 else {
            throw TransportFailure.message("could not create Unix socket: \(posixMessage())")
        }
        defer { Darwin.close(descriptor) }

        var noSigPipe: Int32 = 1
        guard setsockopt(
            descriptor,
            SOL_SOCKET,
            SO_NOSIGPIPE,
            &noSigPipe,
            socklen_t(MemoryLayout.size(ofValue: noSigPipe))
        ) == 0 else {
            throw TransportFailure.message("could not configure Unix socket: \(posixMessage())")
        }
        var timeout = timeval(tv_sec: 2, tv_usec: 0)
        guard setsockopt(
            descriptor,
            SOL_SOCKET,
            SO_RCVTIMEO,
            &timeout,
            socklen_t(MemoryLayout.size(ofValue: timeout))
        ) == 0,
        setsockopt(
            descriptor,
            SOL_SOCKET,
            SO_SNDTIMEO,
            &timeout,
            socklen_t(MemoryLayout.size(ofValue: timeout))
        ) == 0 else {
            throw TransportFailure.message("could not configure IPC timeouts: \(posixMessage())")
        }

        var address = sockaddr_un()
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        address.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(path.utf8) + [0]
        guard pathBytes.count <= MemoryLayout.size(ofValue: address.sun_path) else {
            throw TransportFailure.message("Unix socket path is too long")
        }
        withUnsafeMutableBytes(of: &address.sun_path) { destination in
            destination.copyBytes(from: pathBytes)
        }
        let connected = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(descriptor, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard connected == 0 else {
            if errno == ENOENT || errno == ECONNREFUSED { throw TransportFailure.daemonNotRunning }
            throw TransportFailure.message("could not connect to daemon: \(posixMessage())")
        }

        try frame.withUnsafeBytes { bytes in
            guard let base = bytes.baseAddress else { return }
            var written = 0
            while written < bytes.count {
                let count = Darwin.send(descriptor, base.advanced(by: written), bytes.count - written, 0)
                if count > 0 {
                    written += count
                    continue
                }
                if count < 0, errno == EINTR { continue }
                if count < 0, errno == EAGAIN || errno == EWOULDBLOCK {
                    throw TransportFailure.message("request write timed out")
                }
                throw TransportFailure.message("could not write request: \(posixMessage())")
            }
        }

        var response = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while true {
            let count = recv(descriptor, &buffer, buffer.count, 0)
            if count > 0 {
                for byte in buffer.prefix(count) {
                    response.append(byte)
                    if response.count > Self.maximumResponseBytes {
                        throw TransportFailure.message("response exceeds \(Self.maximumResponseBytes)-byte limit")
                    }
                    if byte == 0x0A {
                        response.removeLast()
                        return response
                    }
                }
                continue
            }
            if count == 0 {
                throw TransportFailure.message("response ended before newline terminator")
            }
            if errno == EINTR { continue }
            if errno == EAGAIN || errno == EWOULDBLOCK {
                throw TransportFailure.message("response read timed out")
            }
            throw TransportFailure.message("could not read response: \(posixMessage())")
        }
    }

    private func posixMessage() -> String {
        String(cString: strerror(errno))
    }
}
