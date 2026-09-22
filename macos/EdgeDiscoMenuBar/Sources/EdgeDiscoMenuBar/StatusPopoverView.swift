import AppKit
import EdgeDiscoIPC
import SwiftUI

extension SanitizedDetection {
    var runningLabel: String { running ? "Running" : "Not running" }
}

struct StatusPopoverView: View {
    @ObservedObject var viewModel: StatusViewModel
    let quitAction: () -> Void
    @State private var isShowingDetections = false

    private var buildMetadata: [(String, String)] {
        let info = Bundle.main.infoDictionary ?? [:]
        return [
            ("Version", info["CFBundleShortVersionString"] as? String ?? "Development"),
            ("Build", info["EdgeDiscoBuildNumber"] as? String ?? "local"),
            ("Tag", info["EdgeDiscoReleaseTag"] as? String ?? "local"),
        ]
    }

    private var formattedLastScan: String {
        guard let iso = viewModel.lastScanTimestamp, !iso.isEmpty else { return "Never" }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = formatter.date(from: iso)
        if date == nil {
            formatter.formatOptions = [.withInternetDateTime]
            date = formatter.date(from: iso)
        }
        guard let validDate = date else { return iso }
        let displayFormatter = DateFormatter()
        displayFormatter.dateStyle = .none
        displayFormatter.timeStyle = .medium
        return displayFormatter.string(from: validDate)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
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
                    Text("Assets").foregroundStyle(.secondary)
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

                Button("View Detections") {
                    isShowingDetections = true
                    Task { await viewModel.loadDetections() }
                }
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
        .sheet(isPresented: $isShowingDetections) {
            DetectionsListView(viewModel: viewModel)
        }
    }
}

struct DetectionsListView: View {
    @ObservedObject var viewModel: StatusViewModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Detected AI Tools")
                    .font(.headline)
                Spacer()
                Button("Done") { dismiss() }
            }

            if viewModel.isLoadingDetections, viewModel.detections.isEmpty {
                ProgressView("Loading detections…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if viewModel.detections.isEmpty {
                VStack(spacing: 10) {
                    Image(systemName: "magnifyingglass")
                        .font(.largeTitle)
                        .foregroundStyle(.secondary)
                    Text("No Detections")
                        .font(.headline)
                    Text("Run a scan to discover AI tools on this Mac.")
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List(viewModel.detections) { detection in
                    VStack(alignment: .leading, spacing: 5) {
                        HStack {
                            Text(detection.name)
                                .font(.headline)
                            Spacer()
                            Text(detection.runningLabel)
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
                    }
                    .padding(.vertical, 3)
                }
                .listStyle(.inset)
            }
        }
        .padding(16)
        .frame(minWidth: 420, minHeight: 300)
    }
}
