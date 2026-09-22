import Foundation

public let edgeDiscoProtocolVersion: UInt16 = 1

public struct IpcRequest: Codable, Equatable, Sendable {
    public let protocolVersion: UInt16
    public let requestID: String
    public let method: String
    public let payload: SettingsUpdateRequest?

    public init(protocolVersion: UInt16, requestID: String, method: String, payload: SettingsUpdateRequest? = nil) {
        self.protocolVersion = protocolVersion
        self.requestID = requestID
        self.method = method
        self.payload = payload
    }

    enum CodingKeys: String, CodingKey {
        case protocolVersion = "protocol_version"
        case requestID = "request_id"
        case method
        case payload
    }
}

public struct DaemonSettings: Codable, Equatable, Sendable {
    public var schemaVersion: UInt32
    public var intervalSeconds: UInt64
    public var otlpEndpoint: String?
    public var otlpBatchSize: Int
    public var exportEnabled: Bool

    public init(schemaVersion: UInt32 = 1, intervalSeconds: UInt64, otlpEndpoint: String?, otlpBatchSize: Int, exportEnabled: Bool) {
        self.schemaVersion = schemaVersion
        self.intervalSeconds = intervalSeconds
        self.otlpEndpoint = otlpEndpoint
        self.otlpBatchSize = otlpBatchSize
        self.exportEnabled = exportEnabled
    }

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case intervalSeconds = "interval_seconds"
        case otlpEndpoint = "otlp_endpoint"
        case otlpBatchSize = "otlp_batch_size"
        case exportEnabled = "export_enabled"
    }
}

public struct SettingsSnapshot: Codable, Equatable, Sendable {
    public let settings: DaemonSettings
    public let revision: String
    public let writable: Bool
}

public struct ExportDiagnostics: Codable, Equatable, Sendable {
    public let queued: Int64
    public let deliveredTotal: Int64
    public let retriedTotal: Int64
    public let failedTotal: Int64
    public let droppedTotal: Int64
    public let lastSuccessAt: String?
    public let lastFailureAt: String?

    enum CodingKeys: String, CodingKey {
        case queued
        case deliveredTotal = "delivered_total"
        case retriedTotal = "retried_total"
        case failedTotal = "failed_total"
        case droppedTotal = "dropped_total"
        case lastSuccessAt = "last_success_at"
        case lastFailureAt = "last_failure_at"
    }
}

public struct ConnectionTestResult: Codable, Equatable, Sendable {
    public let accepted: Bool
    public let status: String
    public let httpStatus: UInt16?

    enum CodingKeys: String, CodingKey {
        case accepted
        case status
        case httpStatus = "http_status"
    }
}

public struct SettingsUpdateRequest: Codable, Equatable, Sendable {
    public let expectedRevision: String
    public let settings: DaemonSettings

    public init(expectedRevision: String, settings: DaemonSettings) {
        self.expectedRevision = expectedRevision
        self.settings = settings
    }

    enum CodingKeys: String, CodingKey {
        case expectedRevision = "expected_revision"
        case settings
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

public struct DetectionsResult: Codable, Equatable, Sendable {
    public let detections: [SanitizedDetection]

    public init(detections: [SanitizedDetection]) {
        self.detections = detections
    }
}

public struct SanitizedDetection: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let productID: String?
    public let kind: String
    public let name: String
    public let vendor: String
    public let version: String?
    public let running: Bool
    public let present: Bool?
    public let lastSeen: String?

    public init(
        id: String = UUID().uuidString,
        productID: String? = nil,
        kind: String,
        name: String,
        vendor: String,
        version: String?,
        running: Bool,
        present: Bool?,
        lastSeen: String?
    ) {
        self.id = id
        self.productID = productID
        self.kind = kind
        self.name = name
        self.vendor = vendor
        self.version = version
        self.running = running
        self.present = present
        self.lastSeen = lastSeen
    }

    enum CodingKeys: String, CodingKey {
        case id
        case productID = "product_id"
        case kind
        case name
        case vendor
        case version
        case running
        case present
        case lastSeen = "last_seen"
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decodeIfPresent(String.self, forKey: .id) ?? UUID().uuidString
        productID = try values.decodeIfPresent(String.self, forKey: .productID)
        kind = try values.decode(String.self, forKey: .kind)
        name = try values.decode(String.self, forKey: .name)
        vendor = try values.decode(String.self, forKey: .vendor)
        version = try values.decodeIfPresent(String.self, forKey: .version)
        running = try values.decode(Bool.self, forKey: .running)
        present = try values.decodeIfPresent(Bool.self, forKey: .present)
        lastSeen = try values.decodeIfPresent(String.self, forKey: .lastSeen)
    }

    public static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.productID == rhs.productID
            && lhs.kind == rhs.kind
            && lhs.name == rhs.name
            && lhs.vendor == rhs.vendor
            && lhs.version == rhs.version
            && lhs.running == rhs.running
            && lhs.present == rhs.present
            && lhs.lastSeen == rhs.lastSeen
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
