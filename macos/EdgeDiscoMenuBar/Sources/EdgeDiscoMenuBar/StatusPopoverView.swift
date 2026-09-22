import AppKit
import SwiftUI

struct StatusPopoverView: View {
    @ObservedObject var viewModel: StatusViewModel
    let quitAction: () -> Void

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

            Divider()

            HStack {
                Spacer()
                Button("Quit", action: quitAction)
                    .keyboardShortcut("q")
            }
        }
        .padding(16)
        .frame(width: 320)
    }
}
