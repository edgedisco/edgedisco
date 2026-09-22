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
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text("Choose which daemon's settings to view. My Session is editable; This Mac is managed by an administrator.")
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)

                    Picker("Scope", selection: $scope) {
                        ForEach(InventoryScope.allCases) { Text($0.rawValue).tag($0) }
                    }
                    .pickerStyle(.segmented)
                    .onChange(of: scope) { _ in
                        snapshot = nil
                        diagnostics = nil
                        connectionTestMessage = nil
                        Task { await load() }
                    }

                    if snapshot == nil {
                        GroupBox("Connection") {
                            VStack(alignment: .leading, spacing: 12) {
                                Text(message ?? "Loading settings…")
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                                Button("Retry") { Task { await load() } }
                                    .disabled(busy)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(8)
                        }
                    } else {
                        GroupBox("Scanning and export") {
                            VStack(alignment: .leading, spacing: 16) {
                                Text(scope == .user
                                    ? "Changes to My Session apply without restarting the daemon."
                                    : "This Mac settings are read-only. An administrator can edit the system configuration and restart its daemon.")
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)

                                HStack(alignment: .top, spacing: 20) {
                                    VStack(alignment: .leading, spacing: 5) {
                                        Text("Scan interval (seconds)")
                                        TextField("Seconds", text: $interval)
                                            .disabled(!canApply)
                                    }
                                    VStack(alignment: .leading, spacing: 5) {
                                        Text("Records per batch")
                                        TextField("Records", text: $batchSize)
                                            .disabled(!canApply)
                                    }
                                }
                                Toggle("Export via OTLP", isOn: $enabled)
                                    .disabled(!canApply)
                                VStack(alignment: .leading, spacing: 5) {
                                    Text("OTLP/HTTP logs endpoint")
                                    TextField("https://collector.example/v1/logs", text: $endpoint)
                                        .disabled(!canApply)
                                    Text("Use HTTPS, or HTTP to a loopback endpoint. Saved in your private EdgeDisco configuration.")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                            .textFieldStyle(.roundedBorder)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(8)
                        }

                        GroupBox("Export activity") {
                            VStack(alignment: .leading, spacing: 14) {
                                if let diagnostics {
                                    HStack(spacing: 24) {
                                        metric("Queued", value: diagnostics.queued)
                                        metric("Delivered", value: diagnostics.deliveredTotal)
                                        metric("Retried", value: diagnostics.retriedTotal)
                                        metric("Failed", value: diagnostics.failedTotal)
                                        metric("Dropped", value: diagnostics.droppedTotal)
                                    }
                                    Divider()
                                    Text("Last delivery: \(diagnostics.lastSuccessAt ?? "None recorded")")
                                    Text("Last failure: \(diagnostics.lastFailureAt ?? "None recorded")")
                                } else {
                                    Text(diagnosticsMessage ?? "Loading export activity…")
                                        .foregroundStyle(.secondary)
                                }
                                Button(testingConnection ? "Testing…" : "Test saved OTLP connection") {
                                    Task { await testConnection() }
                                }
                                .disabled(busy || testingConnection || snapshot?.settings.otlpEndpoint == nil)
                                Text("Sends an empty OTLP request. No inventory data is delivered.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                if let connectionTestMessage {
                                    Text(connectionTestMessage)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(8)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(24)
            }
            Divider()
            HStack {
                if let message, snapshot != nil {
                    Text(message)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                } else {
                    Spacer()
                }
                Button("Reload") { Task { await load() } }
                    .disabled(busy || testingConnection)
                Button(busy ? "Applying…" : "Apply") { Task { await apply() } }
                    .disabled(!canApply)
                    .keyboardShortcut(.defaultAction)
            }
            .padding(.horizontal, 24)
            .padding(.vertical, 14)
        }
        .frame(minWidth: 640, minHeight: 480)
        .task { await load() }
    }

    private func metric(_ title: String, value: Int64) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(value.formatted()).font(.title3.weight(.semibold))
        }
        .frame(maxWidth: .infinity, alignment: .leading)
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
            message = "The \(selected.rawValue) daemon is not running."
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
