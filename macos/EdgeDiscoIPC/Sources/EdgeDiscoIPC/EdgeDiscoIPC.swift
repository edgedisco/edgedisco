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

public struct ScanResult: Codable, Equatable, Sendable {
    public let accepted: Bool
    public let assetCount: UInt64

    public init(accepted: Bool, assetCount: UInt64) {
        self.accepted = accepted
        self.assetCount = assetCount
    }

    enum CodingKeys: String, CodingKey {
        case accepted
        case assetCount = "asset_count"
    }
}

public struct SanitizedDetection: Codable, Equatable, Identifiable, Sendable {
    public let kind: String
    public let name: String
    public let vendor: String
    public let version: String?
    public let running: Bool
    public let present: Bool?
    public let lastSeen: String?

    public var id: String {
        [kind, name, vendor, version ?? "", lastSeen ?? ""].joined(separator: "\u{1f}")
    }

    public init(
        kind: String,
        name: String,
        vendor: String,
        version: String?,
        running: Bool,
        present: Bool?,
        lastSeen: String?
    ) {
        self.kind = kind
        self.name = name
        self.vendor = vendor
        self.version = version
        self.running = running
        self.present = present
        self.lastSeen = lastSeen
    }

    enum CodingKeys: String, CodingKey {
        case kind
        case name
        case vendor
        case version
        case running
        case present
        case lastSeen = "last_seen"
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
