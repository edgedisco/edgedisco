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

    init(viewModel: StatusViewModel? = nil) {
        let viewModel = viewModel ?? StatusViewModel()
        self.viewModel = viewModel
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        popover = NSPopover()
        super.init()

        popover.behavior = .transient
        popover.contentSize = NSSize(width: 320, height: 230)
        popover.contentViewController = NSHostingController(
            rootView: StatusPopoverView(
                viewModel: viewModel,
                quitAction: { NSApplication.shared.terminate(nil) }
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
        refreshStatus()
        pollTimer = Timer.scheduledTimer(withTimeInterval: Self.pollInterval, repeats: true) {
            [weak self] _ in
            Task { @MainActor in
                self?.refreshStatus()
            }
        }
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
