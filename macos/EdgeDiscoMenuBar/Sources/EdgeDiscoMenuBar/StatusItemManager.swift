import AppKit
import Combine
import SwiftUI

@MainActor
final class StatusItemManager: NSObject {
    private static let pollInterval: TimeInterval = 10

    private let viewModel: StatusViewModel
    private let statusItem: NSStatusItem
    private let popover: NSPopover
    private var pollTimer: Timer?
    private var stateObservation: AnyCancellable?
    private var inventoryWindow: NSWindow?
    private var settingsWindow: NSWindow?

    init(viewModel: StatusViewModel? = nil) {
        let viewModel = viewModel ?? StatusViewModel()
        self.viewModel = viewModel
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        popover = NSPopover()
        super.init()

        popover.behavior = .transient
        popover.contentViewController = NSHostingController(
            rootView: StatusPopoverView(
                viewModel: viewModel,
                quitAction: { NSApplication.shared.terminate(nil) },
                openInventory: { [weak self] in self?.showInventory() },
                openSettings: { [weak self] in self?.showSettings() }
            )
        )

        if let button = statusItem.button {
            button.target = self
            button.action = #selector(togglePopover(_:))
            button.sendAction(on: [.leftMouseUp])
        }

        stateObservation = viewModel.$state.sink { [weak self] _ in
            self?.updateStatusIcon()
        }
        updateStatusIcon()
    }

    deinit {
        pollTimer?.invalidate()
        NSStatusBar.system.removeStatusItem(statusItem)
    }

    func start() {
        pollTimer?.invalidate()
        refreshStatus()
        pollTimer = Timer.scheduledTimer(withTimeInterval: Self.pollInterval, repeats: true) {
            [weak self] _ in
            Task { @MainActor in
                self?.refreshStatus()
            }
        }
    }

    private func showInventory() {
        popover.performClose(nil)
        if inventoryWindow == nil {
            let window = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 760, height: 560),
                styleMask: [.titled, .closable, .miniaturizable, .resizable],
                backing: .buffered, defer: false
            )
            window.title = "EdgeDisco Inventory"
            window.isReleasedWhenClosed = false
            window.contentViewController = NSHostingController(rootView: DetectionsListView(viewModel: viewModel))
            window.center()
            inventoryWindow = window
        }
        inventoryWindow?.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
        Task { await viewModel.loadDetections() }
    }

    private func showSettings() {
        popover.performClose(nil)
        if settingsWindow == nil {
            let window = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 540, height: 410),
                styleMask: [.titled, .closable, .miniaturizable, .resizable],
                backing: .buffered, defer: false
            )
            window.title = "EdgeDisco Settings"
            window.isReleasedWhenClosed = false
            window.contentViewController = NSHostingController(rootView: SettingsView())
            window.center()
            settingsWindow = window
        }
        settingsWindow?.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    @objc
    private func togglePopover(_ sender: Any?) {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(sender)
        } else {
            refreshStatus()
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        }
    }

    private func refreshStatus() {
        Task { [weak viewModel] in
            await viewModel?.refresh()
        }
    }

    private func updateStatusIcon() {
        guard let button = statusItem.button else { return }
        let symbolName = viewModel.isHealthy ? "circle.fill" : "exclamationmark.triangle.fill"
        let description = viewModel.isHealthy ? "EdgeDisco healthy" : "EdgeDisco warning"
        let image = NSImage(systemSymbolName: symbolName, accessibilityDescription: description)
        image?.isTemplate = true
        button.image = image
        button.toolTip = description
        button.title = image == nil ? (viewModel.isHealthy ? "●" : "⚠︎") : ""
    }
}
