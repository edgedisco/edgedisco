import AppKit
import EdgeDiscoIPC
import SwiftUI

extension SanitizedDetection {
    var runningLabel: String { running ? "Running" : "Not running" }
}

struct StatusPopoverView: View {
    @ObservedObject var viewModel: StatusViewModel
    let quitAction: () -> Void
    let openInventory: () -> Void
    let openSettings: () -> Void

    private var buildMetadata: [(String, String)] {
        let info = Bundle.main.infoDictionary ?? [:]
        return [
            ("Version", info["CFBundleShortVersionString"] as? String ?? "Development"),
            ("Build", info["EdgeDiscoBuildNumber"] as? String ?? "local"),
            ("Tag", info["EdgeDiscoReleaseTag"] as? String ?? "local"),
        ]
    }

    private static let isoFormatterWithFractions: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    private static let isoFormatter: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    private static let displayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .none
        f.timeStyle = .medium
        return f
    }()

    private var formattedLastScan: String {
        guard let iso = viewModel.lastScanTimestamp, !iso.isEmpty else { return "Never" }
        var date = Self.isoFormatterWithFractions.date(from: iso)
        if date == nil {
            date = Self.isoFormatter.date(from: iso)
        }
        guard let validDate = date else { return iso }
        return Self.displayFormatter.string(from: validDate)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            InventoryScopePicker(viewModel: viewModel)
            HStack(spacing: 8) {
                Circle()
                    .fill(viewModel.isHealthy ? Color.green : Color.orange)
                    .frame(width: 8, height: 8)
                    .accessibilityLabel(viewModel.isHealthy ? "Healthy" : "Warning")
                Text(viewModel.isHealthy ? "EdgeDisco is healthy" : "EdgeDisco needs attention")
                    .font(.headline)
            }
            .padding(.top, 2)

            if let statusMessage = viewModel.statusMessage {
                Text(statusMessage)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 6) {
                GridRow {
                    Text("Last scan findings").foregroundStyle(.secondary)
                    Text("\(viewModel.assetCount)")
                        .monospacedDigit()
                        .fontWeight(.medium)
                        .accessibilityLabel("Asset count \(viewModel.assetCount)")
                }
                GridRow {
                    Text("Devices").foregroundStyle(.secondary)
                    Text("\(viewModel.deviceCount)")
                        .monospacedDigit()
                        .fontWeight(.medium)
                        .accessibilityLabel("Device count \(viewModel.deviceCount)")
                }
                GridRow {
                    Text("Last scan").foregroundStyle(.secondary)
                    Text(formattedLastScan)
                        .lineLimit(1)
                        .accessibilityLabel("Last scan \(formattedLastScan)")
                }
            }
            .padding(.vertical, 2)

            Divider()

            Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 4) {
                ForEach(buildMetadata, id: \.0) { label, value in
                    GridRow {
                        Text(label).foregroundStyle(.secondary)
                        Text(value)
                            .textSelection(.enabled)
                            .accessibilityLabel("\(label) \(value)")
                    }
                }
            }

            HStack(spacing: 8) {
                Button {
                    Task { await viewModel.scanNow() }
                } label: {
                    HStack(spacing: 6) {
                        if viewModel.isScanning {
                            ProgressView()
                                .controlSize(.small)
                        }
                        Text(viewModel.isScanning ? "Scanning…" : "Scan Now")
                    }
                }
                .disabled(viewModel.isScanning)

                Button("Open Inventory", action: openInventory)
                Button("Settings…", action: openSettings)
            }

            Divider()

            HStack {
                Spacer()
                Button("Quit", action: quitAction)
                    .keyboardShortcut("q")
            }
        }
        .padding(14)
        .frame(width: 290)
    }
}

struct DetectionsListView: View {
    @ObservedObject var viewModel: StatusViewModel
    @State private var search = ""
    @State private var filter = "All"
    private var visible: [SanitizedDetection] {
        viewModel.detections.filter {
            (search.isEmpty || "\($0.name) \($0.vendor) \($0.kind)".localizedCaseInsensitiveContains(search))
            && (filter == "All" || (filter == "Running" && $0.running)
                || (filter == "Installed" && $0.kind == "application" && $0.present != false)
                || (filter == "Previously seen" && $0.present == false))
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Inventory — \(viewModel.scope.rawValue)")
                    .font(.headline)
                Spacer()
                Button("Refresh") { Task { await viewModel.loadDetections() } }
                    .disabled(viewModel.isLoadingDetections)
                Button(viewModel.isScanning ? "Scanning…" : "Scan Now") {
                    Task { await viewModel.scanNow(); await viewModel.loadDetections() }
                }.disabled(viewModel.isScanning)
            }
            InventoryScopePicker(viewModel: viewModel)
            if let error = viewModel.detectionsError {
                Text(error).foregroundStyle(.orange)
                Text("Previously loaded findings, if shown, may be stale.").font(.caption)
            }
            TextField("Search name, vendor, or type", text: $search)
                .textFieldStyle(.roundedBorder)
            Picker("State", selection: $filter) {
                ForEach(["All", "Installed", "Running", "Previously seen"], id: \.self) { Text($0) }
            }.pickerStyle(.segmented)
            Text("\(visible.count) evidence records • A tool may have installation and process records.")
                .font(.caption).foregroundStyle(.secondary)

            if viewModel.isLoadingDetections, viewModel.detections.isEmpty {
                ProgressView("Loading detections…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if visible.isEmpty {
                VStack(spacing: 10) {
                    Image(systemName: "magnifyingglass")
                        .font(.largeTitle)
                        .foregroundStyle(.secondary)
                    Text(viewModel.detectionsError == nil ? "No Matching Detections" : "Inventory Unavailable")
                        .font(.headline)
                    Text("Run a scan to discover AI tools on this Mac.")
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List(visible) { detection in
                    VStack(alignment: .leading, spacing: 5) {
                        HStack {
                            Text(detection.name)
                                .font(.headline)
                            Spacer()
                            Text(detection.present == false ? "Previously seen" : detection.runningLabel)
                                .font(.caption)
                                .padding(.horizontal, 7)
                                .padding(.vertical, 3)
                                .background(
                                    detection.running ? Color.green.opacity(0.2) : Color.secondary.opacity(0.15),
                                    in: Capsule()
                                )
                                .accessibilityLabel("\(detection.name): \(detection.runningLabel)")
                        }
                        Text([detection.vendor, detection.version].compactMap { $0 }.joined(separator: " · "))
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                        Text("Type: \(detection.kind) • Last seen: \(detection.lastSeen ?? "Unknown")")
                            .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                    }
                    .padding(.vertical, 3)
                }
                .listStyle(.inset)
            }
        }
        .padding(16)
        .frame(minWidth: 560, minHeight: 400)
    }
}

struct InventoryScopePicker: View {
    @ObservedObject var viewModel: StatusViewModel
    var body: some View {
        Picker("Inventory", selection: $viewModel.scope) {
            ForEach(InventoryScope.allCases) { Text($0.rawValue).tag($0) }
        }
        .disabled(viewModel.isScanning || viewModel.isLoadingDetections)
        .onChange(of: viewModel.scope) { _ in
            Task { await viewModel.refresh(); await viewModel.loadDetections() }
        }
    }
}
