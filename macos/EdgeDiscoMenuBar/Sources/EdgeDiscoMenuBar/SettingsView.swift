import EdgeDiscoIPC
import SwiftUI

struct SettingsView: View {
    @State private var scope: InventoryScope = .user
    @State private var snapshot: SettingsSnapshot?
    @State private var diagnostics: ExportDiagnostics?
    @State private var diagnosticsMessage: String?
    @State private var connectionTestMessage: String?
    @State private var testingConnection = false
    @State private var interval = "60"
    @State private var endpoint = ""
    @State private var batchSize = "100"
    @State private var enabled = false
    @State private var message: String?
    @State private var busy = false

    private var canApply: Bool { snapshot?.writable == true && !busy && !testingConnection }

    var body: some View {
        Form {
            Picker("Scope", selection: $scope) {
                ForEach(InventoryScope.allCases) { Text($0.rawValue).tag($0) }
            }
            .onChange(of: scope) { _ in snapshot = nil; diagnostics = nil; connectionTestMessage = nil; Task { await load() } }

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
                Section("Export diagnostics") {
                    if let diagnostics {
                        Text("Queued: \(diagnostics.queued) · Delivered: \(diagnostics.deliveredTotal) · Retried: \(diagnostics.retriedTotal)")
                        Text("Failed: \(diagnostics.failedTotal) · Dropped: \(diagnostics.droppedTotal)")
                        Text("Last delivery: \(diagnostics.lastSuccessAt ?? "None recorded")")
                        Text("Last failure: \(diagnostics.lastFailureAt ?? "None recorded")")
                    } else {
                        Text(diagnosticsMessage ?? "Loading export diagnostics…")
                            .foregroundStyle(.secondary)
                    }
                }
                HStack {
                    Button(testingConnection ? "Testing…" : "Test saved OTLP connection") {
                        Task { await testConnection() }
                    }
                    .disabled(busy || testingConnection || snapshot?.settings.otlpEndpoint == nil)
                    Text("Sends an empty OTLP request; does not deliver inventory data.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if let connectionTestMessage {
                    Text(connectionTestMessage).foregroundStyle(.secondary)
                }
                HStack {
                    Button("Reload") { Task { await load() } }.disabled(busy || testingConnection)
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
        let client = EdgeDiscoClient(socketPath: path)
        async let settingsResult = client.settings()
        async let diagnosticsResult = client.exportDiagnostics()
        let result = await settingsResult
        let exportResult = await diagnosticsResult
        guard scope == selected else { return }
        switch exportResult {
        case let .connected(value):
            diagnostics = value
            diagnosticsMessage = nil
        case .daemonNotRunning:
            diagnostics = nil
            diagnosticsMessage = "The selected daemon is not running."
        case let .protocolError(error):
            diagnostics = nil
            diagnosticsMessage = error
        }
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

    private func testConnection() async {
        guard snapshot?.settings.otlpEndpoint != nil else { return }
        let selected = scope
        testingConnection = true
        connectionTestMessage = nil
        defer { testingConnection = false }
        let result = await EdgeDiscoClient(socketPath: selected.socketPath).testOTLPConnection()
        guard scope == selected else { return }
        switch result {
        case let .connected(value):
            if value.accepted {
                connectionTestMessage = "Collector accepted the empty probe. No inventory data was delivered."
            } else if value.status == "http_rejected", let code = value.httpStatus {
                connectionTestMessage = "Collector rejected the empty probe (HTTP \(code)). No inventory data was delivered."
            } else {
                connectionTestMessage = "Connection test failed: \(value.status.replacingOccurrences(of: "_", with: " ")). No inventory data was delivered."
            }
        case .daemonNotRunning:
            connectionTestMessage = "The selected daemon is not running."
        case let .protocolError(error):
            connectionTestMessage = error
        }
    }
}
