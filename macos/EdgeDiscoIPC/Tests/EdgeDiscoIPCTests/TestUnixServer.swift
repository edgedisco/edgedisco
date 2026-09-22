import Darwin
import Foundation
import XCTest
@testable import EdgeDiscoIPC

final class TestUnixServer {
    typealias Responder = (Data) -> Data?

    let path: String
    private let listener: Int32
    private let responder: Responder
    private let holdOpen: TimeInterval
    private let queue = DispatchQueue(label: "EdgeDiscoIPCTests.TestUnixServer")
    private let lock = NSLock()
    private let finished = DispatchSemaphore(value: 0)

    private(set) var connectionCount = 0
    private(set) var receivedFrame = Data()
    private(set) var clientClosedAfterResponse = false

    init(holdOpen: TimeInterval = 0, responder: @escaping Responder) throws {
        path = "/tmp/edgedisco-ipc-\(UUID().uuidString).sock"
        self.responder = responder
        self.holdOpen = holdOpen
        listener = socket(AF_UNIX, SOCK_STREAM, 0)
        guard listener >= 0 else { throw POSIXError(.ENOTSOCK) }

        var address = sockaddr_un()
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        address.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(path.utf8) + [0]
        guard pathBytes.count <= MemoryLayout.size(ofValue: address.sun_path) else {
            Darwin.close(listener)
            throw POSIXError(.ENAMETOOLONG)
        }
        withUnsafeMutableBytes(of: &address.sun_path) { destination in
            destination.copyBytes(from: pathBytes)
        }
        let bindResult = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(listener, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard bindResult == 0, Darwin.listen(listener, 1) == 0 else {
            let code = errno
            Darwin.close(listener)
            throw POSIXError(POSIXErrorCode(rawValue: code) ?? .EIO)
        }

        queue.async { [self] in serveOneConnection() }
    }

    deinit {
        Darwin.close(listener)
        unlink(path)
    }

    func wait(timeout: TimeInterval = 2) -> Bool {
        finished.wait(timeout: .now() + timeout) == .success
    }

    func snapshot() -> (connections: Int, frame: Data, clientClosed: Bool) {
        lock.lock()
        defer { lock.unlock() }
        return (connectionCount, receivedFrame, clientClosedAfterResponse)
    }

    private func serveOneConnection() {
        defer { finished.signal() }
        let client = accept(listener, nil, nil)
        guard client >= 0 else { return }
        defer { Darwin.close(client) }

        var noSigPipe: Int32 = 1
        setsockopt(client, SOL_SOCKET, SO_NOSIGPIPE, &noSigPipe, socklen_t(MemoryLayout.size(ofValue: noSigPipe)))

        lock.lock()
        connectionCount += 1
        lock.unlock()

        var frame = Data()
        var byte: UInt8 = 0
        while recv(client, &byte, 1, 0) == 1 {
            frame.append(byte)
            if byte == 0x0A { break }
        }
        lock.lock()
        receivedFrame = frame
        lock.unlock()

        if let response = responder(frame) {
            response.withUnsafeBytes { bytes in
                var sent = 0
                while sent < bytes.count {
                    let count = Darwin.send(client, bytes.baseAddress!.advanced(by: sent), bytes.count - sent, 0)
                    if count <= 0 { break }
                    sent += count
                }
            }
        } else if holdOpen > 0 {
            Thread.sleep(forTimeInterval: holdOpen)
        }

        var timeout = timeval(tv_sec: 1, tv_usec: 0)
        setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout.size(ofValue: timeout)))
        let closed = recv(client, &byte, 1, 0) == 0
        lock.lock()
        clientClosedAfterResponse = closed
        lock.unlock()
    }
}

func responseFrame(request: Data, resultJSON: String, protocolVersion: UInt16 = 1) -> Data {
    let requestData = request.last == 0x0A ? request.dropLast() : request[request.startIndex...]
    let decoded = try! JSONDecoder().decode(IpcRequest.self, from: Data(requestData))
    var frame = Data(#"{"protocol_version":\#(protocolVersion),"request_id":"\#(decoded.requestID)","ok":true,"result":\#(resultJSON)}"#.utf8)
    frame.append(0x0A)
    return frame
}
