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

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Circle()
                    .fill(viewModel.isHealthy ? Color.green : Color.orange)
                    .frame(width: 10, height: 10)
                    .accessibilityLabel(viewModel.isHealthy ? "Healthy" : "Warning")
                Text(viewModel.isHealthy ? "EdgeDisco is healthy" : "EdgeDisco needs attention")
                    .font(.headline)
            }

            if let statusMessage = viewModel.statusMessage {
                Text(statusMessage)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 8) {
                GridRow {
                    Text("Assets")
                    Text("\(viewModel.assetCount)")
                        .monospacedDigit()
                        .accessibilityLabel("Asset count \(viewModel.assetCount)")
                }
                GridRow {
                    Text("Devices")
                    Text("\(viewModel.deviceCount)")
                        .monospacedDigit()
                        .accessibilityLabel("Device count \(viewModel.deviceCount)")
                }
                GridRow {
                    Text("Last scan")
                    Text(viewModel.lastScanTimestamp ?? "Never")
                        .lineLimit(1)
                        .accessibilityLabel("Last scan \(viewModel.lastScanTimestamp ?? "Never")")
                }
            }

            HStack {
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
        .padding(16)
        .frame(width: 320)
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
