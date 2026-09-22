@testable import EdgeDiscoMenuBar
import XCTest

final class SettingsNumericInputTests: XCTestCase {
    func testRejectsMalformedAndNonPositiveNumbersInEitherField() {
        for input in ["1.5", "60abc", "abc", "", "0", "-1", " 60", "60 ", "18446744073709551616"] {
            XCTAssertNil(SettingsNumericInput(interval: input, batchSize: "100"), input)
            XCTAssertNil(SettingsNumericInput(interval: "60", batchSize: input), input)
        }
        XCTAssertNil(SettingsNumericInput(interval: "60", batchSize: String(UInt64.max)))
    }

    func testAcceptsWholeNumbersWithoutNarrowingTheInterval() throws {
        let normal = try XCTUnwrap(SettingsNumericInput(interval: "60", batchSize: "100"))
        XCTAssertEqual(normal.intervalSeconds, 60)
        XCTAssertEqual(normal.batchSize, 100)
        let boundary = try XCTUnwrap(SettingsNumericInput(interval: String(UInt64.max), batchSize: String(Int.max)))
        XCTAssertEqual(boundary.intervalSeconds, UInt64.max)
        XCTAssertEqual(boundary.batchSize, Int.max)
    }
}
