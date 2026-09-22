import EdgeDiscoIPC
import SwiftUI

struct SettingsView: View {
    @State private var scope: InventoryScope = .user
    @State private var snapshot: SettingsSnapshot?
    @State private var interval = "60"
    @State private var endpoint = ""
    @State private var batchSize = "100"
    @State private var enabled = false
    @State private var message: String?
    @State private var busy = false

    private var canApply: Bool { snapshot?.writable == true && !busy }

    var body: some View {
        Form {
            Picker("Scope", selection: $scope) {
                ForEach(InventoryScope.allCases) { Text($0.rawValue).tag($0) }
            }
            .onChange(of: scope) { _ in snapshot = nil; Task { await load() } }

            if snapshot == nil {
                Text(message ?? "Loading settings…")
                    .foregroundStyle(.secondary)
                Button("Retry") { Task { await load() } }
                    .disabled(busy)
            } else {
                Text(scope == .user ? "Changes to My Session apply without restarting the daemon." : "This Mac settings are read-only here. An administrator can edit the system config and restart its daemon.")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                TextField("Scan interval (seconds)", text: $interval)
                    .disabled(!canApply)
                Toggle("Export via OTLP", isOn: $enabled)
                    .disabled(!canApply)
                TextField("OTLP/HTTP logs endpoint", text: $endpoint)
                    .textFieldStyle(.roundedBorder)
                    .disabled(!canApply)
                TextField("Records per batch", text: $batchSize)
                    .disabled(!canApply)

                Text("Use HTTPS, or HTTP to a loopback endpoint. The endpoint is saved in your private EdgeDisco configuration.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if let message { Text(message).foregroundStyle(.secondary) }
                HStack {
                    Button("Reload") { Task { await load() } }.disabled(busy)
                    Spacer()
                    Button(busy ? "Applying…" : "Apply") { Task { await apply() } }
                        .disabled(!canApply)
                }
            }
        }
        .padding(20)
        .frame(minWidth: 500, minHeight: 350)
        .task { await load() }
    }

    private func load() async {
        let selected = scope
        busy = true
        defer { busy = false }
        let path = selected.socketPath
        let result = await EdgeDiscoClient(socketPath: path).settings()
        guard scope == selected else { return }
        switch result {
        case let .connected(value):
            snapshot = value
            interval = String(value.settings.intervalSeconds)
            endpoint = value.settings.otlpEndpoint ?? ""
            batchSize = String(value.settings.otlpBatchSize)
            enabled = value.settings.exportEnabled
            message = nil
        case .daemonNotRunning:
            snapshot = nil
            message = "The (selected.rawValue) daemon is not running."
        case let .protocolError(error):
            snapshot = nil
            message = error
        }
    }

    private func apply() async {
        guard let snapshot, snapshot.writable else { return }
        guard let numbers = SettingsNumericInput(interval: interval, batchSize: batchSize) else {
            message = "Interval and batch size must be positive whole numbers."
            return
        }
        let trimmed = endpoint.trimmingCharacters(in: .whitespacesAndNewlines)
        if enabled && trimmed.isEmpty {
            message = "Enter an OTLP endpoint before enabling export."
            return
        }
        let settings = DaemonSettings(intervalSeconds: numbers.intervalSeconds, otlpEndpoint: trimmed.isEmpty ? nil : trimmed, otlpBatchSize: numbers.batchSize, exportEnabled: enabled)
        let selected = scope
        busy = true
        defer { busy = false }
        let path = selected.socketPath
        let result = await EdgeDiscoClient(socketPath: path).applySettings(settings, expectedRevision: snapshot.revision)
        guard scope == selected else { return }
        switch result {
        case let .connected(value):
            self.snapshot = value
            message = "Settings saved and applied."
        case .daemonNotRunning:
            message = "The daemon stopped before settings could be saved."
        case let .protocolError(error):
            message = error
        }
    }
}
