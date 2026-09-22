import EdgeDiscoIPC
@testable import EdgeDiscoMenuBar
import XCTest

@MainActor
final class InventoryOverviewTests: XCTestCase {
    private func detection(id: String, productID: String?, name: String = "Agent") -> SanitizedDetection {
        SanitizedDetection(id: id, productID: productID, kind: "application", name: name,
            vendor: "Example", version: nil, running: false, present: true, lastSeen: nil)
    }

    func testPartialSourceFailureKeepsAvailableInventoryVisible() async {
        let finding = detection(id: "one", productID: "known")
        let model = InventoryOverviewModel(fetcher: { scope in
            scope == .user ? .success([finding]) : .failure(.daemonNotRunning)
        })
        await model.refresh()
        XCTAssertEqual(model.availableSourceCount, 1)
        XCTAssertEqual(model.products.count, 1)
        XCTAssertEqual(model.findings.count, 1)
        XCTAssertEqual(model.system, .unavailable("Daemon not running"))
    }

    func testKnownProductCombinesAcrossSourcesButUnrelatedFindingsDoNot() async {
        let userRows = [detection(id: "user-agent", productID: "known"),
            detection(id: "user-other", productID: nil, name: "Other")]
        let systemRows = [detection(id: "system-agent", productID: "known"),
            detection(id: "system-other", productID: nil, name: "Other")]
        let model = InventoryOverviewModel(fetcher: { scope in
            .success(scope == .user ? userRows : systemRows)
        })
        await model.refresh()
        XCTAssertEqual(model.availableSourceCount, 2)
        XCTAssertEqual(model.findings.count, 4)
        XCTAssertEqual(model.products.count, 3)
        XCTAssertEqual(model.products.first { $0.name == "Agent" }?.sourceCount, 2)
    }

    func testBothUnavailableIsNotAnEmptySuccessfulInventory() async {
        let model = InventoryOverviewModel(fetcher: { _ in .failure(.daemonNotRunning) })
        await model.refresh()
        XCTAssertEqual(model.availableSourceCount, 0)
        XCTAssertEqual(model.user, .unavailable("Daemon not running"))
        XCTAssertEqual(model.system, .unavailable("Daemon not running"))
    }
}
