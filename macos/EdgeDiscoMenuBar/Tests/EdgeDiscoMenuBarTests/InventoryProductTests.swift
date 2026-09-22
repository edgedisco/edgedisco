import EdgeDiscoIPC
@testable import EdgeDiscoMenuBar
import XCTest

final class InventoryProductTests: XCTestCase {
    private func finding(
        id: String,
        productID: String?,
        name: String,
        kind: String,
        running: Bool = false,
        present: Bool? = true
    ) -> SanitizedDetection {
        SanitizedDetection(id: id, productID: productID, kind: kind, name: name,
            vendor: "Example", version: nil, running: running, present: present, lastSeen: nil)
    }

    func testCatalogProductCombinesInstalledAndRunningEvidence() {
        let installed = finding(id: "install", productID: "catalog-key", name: "Agent", kind: "application")
        let process = finding(id: "process", productID: "catalog-key", name: "Agent", kind: "process", running: true)
        let products = InventoryProduct.group([process, installed])
        XCTAssertEqual(products.count, 1)
        XCTAssertEqual(products[0].evidence.count, 2)
        XCTAssertTrue(products[0].installed)
        XCTAssertTrue(products[0].running)
    }

    func testUnknownProductsRemainSeparateEvenWithSameDisplayName() {
        let first = finding(id: "one", productID: nil, name: "Unknown", kind: "process")
        let second = finding(id: "two", productID: nil, name: "Unknown", kind: "process")
        XCTAssertEqual(InventoryProduct.group([first, second]).count, 2)
    }

    func testHistoricalProductIsNotPresentedAsInstalledOrRunning() {
        let old = finding(id: "old", productID: "known", name: "Agent", kind: "application", present: false)
        let product = InventoryProduct.group([old])[0]
        XCTAssertTrue(product.previouslySeen)
        XCTAssertFalse(product.installed)
        XCTAssertFalse(product.running)
    }
}
