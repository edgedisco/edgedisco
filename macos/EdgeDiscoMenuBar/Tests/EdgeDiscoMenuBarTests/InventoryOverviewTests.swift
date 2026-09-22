import EdgeDiscoIPC
@testable import EdgeDiscoMenuBar
import XCTest

@MainActor
final class InventoryOverviewTests: XCTestCase {
    private func detection(id: String, productID: String?, name: String = "Agent",
                           kind: String = "application", running: Bool = false,
                           present: Bool? = true) -> SanitizedDetection {
        SanitizedDetection(id: id, productID: productID, kind: kind, name: name,
            vendor: "Example", version: nil, running: running, present: present, lastSeen: nil)
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

    func testScopeFilterAndProductStateTotals() async {
        let userRows = [
            detection(id: "installed", productID: "shared"),
            detection(id: "runtime", productID: "shared", kind: "process", running: true),
            detection(id: "old", productID: "historic", present: false),
        ]
        let systemRows = [detection(id: "system", productID: "shared")]
        let model = InventoryOverviewModel(fetcher: { scope in
            .success(scope == .user ? userRows : systemRows)
        })
        await model.refresh()

        XCTAssertEqual(model.findings(in: nil).count, 4)
        XCTAssertEqual(model.findings(in: .user).count, 3)
        XCTAssertEqual(model.findings(in: .system).count, 1)
        XCTAssertEqual(model.products(in: .user).count, 2)
        XCTAssertEqual(model.products(in: .system).count, 1)
        XCTAssertEqual(model.products(in: nil).first { $0.id == "product:shared" }?.sourceCount, 2)
        let totals = OverviewTotals(model.products(in: nil))
        XCTAssertEqual(totals.products, 2)
        XCTAssertEqual(totals.installed, 1)
        XCTAssertEqual(totals.running, 1)
        XCTAssertEqual(totals.previouslySeen, 1)
    }

    func testUnknownFindingsWithMatchingIDsStaySeparateAcrossScopes() {
        let finding = detection(id: "same", productID: nil)
        let products = OverviewProduct.group([
            SourcedFinding(scope: .user, detection: finding),
            SourcedFinding(scope: .system, detection: finding),
        ])
        XCTAssertEqual(products.count, 2)
        XCTAssertTrue(products.allSatisfy { $0.sourceCount == 1 })
    }
}
