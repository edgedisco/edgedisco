import EdgeDiscoIPC
import Foundation
import SwiftUI

enum InventorySourceState: Equatable {
    case loading
    case available([SanitizedDetection])
    case unavailable(String)

    var findings: [SanitizedDetection]? {
        if case let .available(rows) = self { return rows }
        return nil
    }
}

struct SourcedFinding: Identifiable {
    let scope: InventoryScope
    let detection: SanitizedDetection
    var id: String { "\(scope.rawValue):\(detection.id)" }
}

struct OverviewProduct: Identifiable {
    let id: String
    let name: String
    let vendor: String
    let findings: [SourcedFinding]

    var running: Bool { findings.contains { $0.detection.running } }
    var installed: Bool {
        findings.contains { $0.detection.kind == "application" && $0.detection.present != false }
    }
    var previouslySeen: Bool { findings.allSatisfy { $0.detection.present == false } }
    var sourceCount: Int { Set(findings.map(\.scope)).count }

    static func group(_ findings: [SourcedFinding]) -> [OverviewProduct] {
        let grouped = Dictionary(grouping: findings) { row in
            row.detection.productID.map { "product:\($0)" }
                ?? "evidence:\(row.scope.rawValue):\(row.detection.id)"
        }
        return grouped.compactMap { id, rows in
            guard let first = rows.first else { return nil }
            return OverviewProduct(id: id, name: first.detection.name,
                vendor: first.detection.vendor,
                findings: rows.sorted { $0.id < $1.id })
        }.sorted {
            ($0.name.localizedLowercase, $0.vendor.localizedLowercase, $0.id)
                < ($1.name.localizedLowercase, $1.vendor.localizedLowercase, $1.id)
        }
    }
}

struct OverviewTotals: Equatable {
    let products: Int
    let installed: Int
    let running: Int
    let previouslySeen: Int

    init(_ products: [OverviewProduct]) {
        self.products = products.count
        installed = products.filter(\.installed).count
        running = products.filter(\.running).count
        previouslySeen = products.filter(\.previouslySeen).count
    }
}

enum OverviewScopeFilter: String, CaseIterable, Identifiable {
    case both = "Both Scopes"
    case user = "My Session"
    case system = "This Mac"

    var id: String { rawValue }
    var scope: InventoryScope? {
        switch self {
        case .both: nil
        case .user: .user
        case .system: .system
        }
    }
}

@MainActor
final class InventoryOverviewModel: ObservableObject {
    typealias Fetcher = @Sendable (InventoryScope) async -> Result<[SanitizedDetection], IpcError>

    @Published private(set) var user: InventorySourceState = .loading
    @Published private(set) var system: InventorySourceState = .loading
    private let fetcher: Fetcher
    private var generation = 0

    init(fetcher: Fetcher? = nil) {
        self.fetcher = fetcher ?? { scope in
            await EdgeDiscoClient(socketPath: scope.socketPath).detections()
        }
    }

    var isLoading: Bool { user == .loading || system == .loading }
    var availableSourceCount: Int { [user, system].filter { $0.findings != nil }.count }
    var findings: [SourcedFinding] {
        (user.findings ?? []).map { SourcedFinding(scope: .user, detection: $0) }
            + (system.findings ?? []).map { SourcedFinding(scope: .system, detection: $0) }
    }
    var products: [OverviewProduct] { OverviewProduct.group(findings) }

    func state(for scope: InventoryScope) -> InventorySourceState {
        scope == .user ? user : system
    }

    func findings(in scope: InventoryScope?) -> [SourcedFinding] {
        findings.filter { scope == nil || $0.scope == scope }
    }

    func products(in scope: InventoryScope?) -> [OverviewProduct] {
        OverviewProduct.group(findings(in: scope))
    }

    func refresh() async {
        generation += 1
        let current = generation
        user = .loading
        system = .loading
        async let userResult = fetcher(.user)
        async let systemResult = fetcher(.system)
        let (userResponse, systemResponse) = await (userResult, systemResult)
        guard current == generation else { return }
        user = Self.state(from: userResponse)
        system = Self.state(from: systemResponse)
    }

    private static func state(from result: Result<[SanitizedDetection], IpcError>) -> InventorySourceState {
        switch result {
        case let .success(rows): .available(rows)
        case .failure(.daemonNotRunning): .unavailable("Daemon not running")
        case let .failure(.protocolError(message)): .unavailable(message)
        }
    }
}

struct InventoryOverviewView: View {
    @StateObject private var model = InventoryOverviewModel()
    @State private var search = ""
    @State private var scopeFilter: OverviewScopeFilter = .both

    private var scopedProducts: [OverviewProduct] { model.products(in: scopeFilter.scope) }
    private var totals: OverviewTotals { OverviewTotals(scopedProducts) }

    private var visible: [OverviewProduct] {
        scopedProducts.filter {
            search.isEmpty || "\($0.name) \($0.vendor)".localizedCaseInsensitiveContains(search)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Inventory Overview").font(.headline)
                Spacer()
                Button("Refresh Both") { Task { await model.refresh() } }
                    .disabled(model.isLoading)
            }
            HStack(spacing: 16) {
                sourceSummary("My Session", state: model.user)
                sourceSummary("This Mac", state: model.system)
            }
            Picker("Scope", selection: $scopeFilter) {
                ForEach(OverviewScopeFilter.allCases) { scope in
                    Text(scope.rawValue).tag(scope)
                }
            }
            .pickerStyle(.segmented)
            if model.isLoading {
                ProgressView("Loading both inventory sources…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let scope = scopeFilter.scope, model.state(for: scope).findings == nil {
                Text("\(scope.rawValue) is unavailable. Select another scope or retry.")
                    .foregroundStyle(.orange)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if model.availableSourceCount == 0 {
                Text("Neither inventory source is available. Check the daemon status and retry.")
                    .foregroundStyle(.orange)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                if scopeFilter == .both && model.availableSourceCount == 1 {
                    Text("Partial overview: one source is unavailable. Totals include only the source shown above.")
                        .foregroundStyle(.orange).font(.caption)
                }
                Text("\(totals.products) grouped entries · \(totals.installed) installed · \(totals.running) running · \(totals.previouslySeen) previously seen")
                    .font(.subheadline)
                Text("\(model.findings(in: scopeFilter.scope).count) source findings · States can overlap; totals are before search.")
                    .font(.caption).foregroundStyle(.secondary)
                TextField("Search products", text: $search)
                    .textFieldStyle(.roundedBorder)
                if visible.isEmpty {
                    Text(search.isEmpty ? "No findings yet. Run a scan in Scope Details." : "No matching products.")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    List(visible) { product in
                        DisclosureGroup {
                            ForEach(product.findings) { row in
                                Text("\(row.scope.rawValue) · \(row.detection.kind) · \(row.detection.running ? "Running" : "Not running")")
                                    .font(.subheadline)
                            }
                        } label: {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(product.name).font(.headline)
                                Text("\(product.vendor) · \(product.sourceCount) \(product.sourceCount == 1 ? "source" : "sources") · \(product.findings.count) findings · \(product.running ? "Running" : product.installed ? "Installed" : product.previouslySeen ? "Previously seen" : "Observed")")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                    .listStyle(.inset)
                }
            }
        }
        .padding(16)
        .frame(minWidth: 560, minHeight: 400)
        .task { await model.refresh() }
    }

    private func sourceSummary(_ title: String, state: InventorySourceState) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.subheadline).fontWeight(.medium)
            switch state {
            case .loading: Text("Loading…").foregroundStyle(.secondary)
            case let .available(rows): Text("\(rows.count) findings").foregroundStyle(.secondary)
            case let .unavailable(message): Text(message).foregroundStyle(.orange)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(Color.secondary.opacity(0.1), in: RoundedRectangle(cornerRadius: 8))
    }
}
