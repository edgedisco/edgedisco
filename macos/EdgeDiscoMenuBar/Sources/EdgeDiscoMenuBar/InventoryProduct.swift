import EdgeDiscoIPC
import Foundation

/// A catalog-matched product can have several installation and runtime findings.
/// Findings without catalog identity intentionally remain individual entries.
struct InventoryProduct: Identifiable, Equatable {
    let id: String
    let name: String
    let vendor: String
    let evidence: [SanitizedDetection]

    var running: Bool { evidence.contains(where: \.running) }
    var installed: Bool { evidence.contains { $0.kind == "application" && $0.present != false } }
    var previouslySeen: Bool { evidence.allSatisfy { $0.present == false } }
    var version: String? { evidence.compactMap(\.version).first }
    var lastSeen: String? { evidence.compactMap(\.lastSeen).max() }

    func matches(_ search: String) -> Bool {
        search.isEmpty || "\(name) \(vendor)".localizedCaseInsensitiveContains(search)
            || evidence.contains { $0.kind.localizedCaseInsensitiveContains(search) }
    }

    static func group(_ detections: [SanitizedDetection]) -> [InventoryProduct] {
        let grouped = Dictionary(grouping: detections) { detection in
            detection.productID.map { "product:\($0)" } ?? "evidence:\(detection.id)"
        }
        return grouped.compactMap { id, rows in
            guard let first = rows.first else { return nil }
            return InventoryProduct(
                id: id,
                name: first.name,
                vendor: first.vendor,
                evidence: rows.sorted { ($0.kind, $0.id) < ($1.kind, $1.id) }
            )
        }.sorted {
            ($0.name.localizedLowercase, $0.vendor.localizedLowercase, $0.id)
                < ($1.name.localizedLowercase, $1.vendor.localizedLowercase, $1.id)
        }
    }
}
