import SwiftUI
import UIKit
import XCTest

@testable import Hardcharger

/// gizmodata/hardcharger#57, checked on iOS: a boxed result table keeps its shape in the console.
/// Android's console drew box-drawing characters wider than its text; the iOS console's font
/// (SF Mono) must have them one cell wide at every Dynamic Type size.
@MainActor
final class ConsoleLayoutTests: XCTestCase {
    private let sizes: [DynamicTypeSize] = [.xSmall, .large, .xxxLarge, .accessibility3, .accessibility5]

    private func width(_ text: String, _ font: UIFont) -> CGFloat {
        (text as NSString).size(withAttributes: [.font: font]).width
    }

    func testBoxDrawingIsAsWideAsTheTextAtEveryTypeSize() {
        let glyphs = ["x", "─", "│", "┌", "┬", "┐", "├", "┼", "┤", "└", "┴", "┘", "═", "║", "╭", "…"]
        for size in sizes {
            let font = SQLConsoleScreen.font(for: size)
            let text = width(String(repeating: "x", count: 40), font)
            for glyph in glyphs {
                XCTAssertEqual(width(String(repeating: glyph, count: 40), font), text, accuracy: 0.5,
                               "40 × \(glyph) at \(size)")
            }
        }
    }
}
