import Foundation

public let edgeDiscoProtocolVersion: UInt16 = 1

public struct IpcRequest: Codable, Equatable, Sendable {
    public let protocolVersion: UInt16
    public let requestID: String
    public let method: String

    public init(protocolVersion: UInt16, requestID: String, method: String) {
        self.protocolVersion = protocolVersion
        self.requestID = requestID
        self.method = method
    }

    enum CodingKeys: String, CodingKey {
        case protocolVersion = "protocol_version"
        case requestID = "request_id"
        case method
    }
}

public struct IpcResponse<Result: Codable & Equatable & Sendable>: Codable, Equatable, Sendable {
    public let protocolVersion: UInt16
    public let requestID: String?
    public let ok: Bool
    public let result: Result?
    public let error: ProtocolError?

    public init(
        protocolVersion: UInt16,
        requestID: String?,
        ok: Bool,
        result: Result?,
        error: ProtocolError?
    ) {
        self.protocolVersion = protocolVersion
        self.requestID = requestID
        self.ok = ok
        self.result = result
        self.error = error
    }

    enum CodingKeys: String, CodingKey {
        case protocolVersion = "protocol_version"
        case requestID = "request_id"
        case ok
        case result
        case error
    }
}

public struct ProtocolError: Codable, Equatable, Sendable {
    public let code: String
    public let message: String

    public init(code: String, message: String) {
        self.code = code
        self.message = message
    }
}

public struct StatusResult: Codable, Equatable, Sendable {
    public let healthy: Bool
    public let startedAt: String
    public let lastScanAt: String?
    public let lastScanAssetCount: Int?
    public let deviceCount: Int
    public let detectionCount: Int

    public init(
        healthy: Bool,
        startedAt: String,
        lastScanAt: String?,
        lastScanAssetCount: Int?,
        deviceCount: Int,
        detectionCount: Int
    ) {
        self.healthy = healthy
        self.startedAt = startedAt
        self.lastScanAt = lastScanAt
        self.lastScanAssetCount = lastScanAssetCount
        self.deviceCount = deviceCount
        self.detectionCount = detectionCount
    }

    enum CodingKeys: String, CodingKey {
        case healthy
        case startedAt = "started_at"
        case lastScanAt = "last_scan_at"
        case lastScanAssetCount = "last_scan_asset_count"
        case deviceCount = "device_count"
        case detectionCount = "detection_count"
    }
}

public struct NegotiateResult: Codable, Equatable, Sendable {
    public let protocolVersion: UInt16
    public let supportedVersions: [UInt16]

    public init(protocolVersion: UInt16, supportedVersions: [UInt16]) {
        self.protocolVersion = protocolVersion
        self.supportedVersions = supportedVersions
    }

    enum CodingKeys: String, CodingKey {
        case protocolVersion = "protocol_version"
        case supportedVersions = "supported_versions"
    }
}
