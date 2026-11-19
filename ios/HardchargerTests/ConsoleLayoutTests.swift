import SwiftUI
import UIKit
import XCTest

@testable import Hardcharger

/// gizmodata/hardcharger#57, checked on iOS: a boxed result table keeps its shape in the console.
/// Android's console drew box-drawing characters wider than its text; the iOS console's font
/// (SF Mono) must have them one cell wide at every Dynamic Type size, and a wide table must never
/// wrap (each result scrolls sideways instead).
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

    /// A table 40 columns wide (about 500 characters a line) on a phone: the result's text view
    /// is as wide as its longest line inside a scroll view as wide as the phone, so every line is
    /// one line fragment (no wrap), every border line is as wide as the rows, and it scrolls
    /// sideways by the difference.
    func testAWideTableNeverWrapsAndItsBordersMeetItsRows() {
        let columns = 40
        let cell = String(repeating: "─", count: 11)
        let top = "┌" + Array(repeating: cell, count: columns).joined(separator: "┬") + "┐"
        let names = "│" + (0..<columns).map { " col_\(String(format: "%05d", $0)) " }.joined(separator: "│") + "│"
        let middle = "├" + Array(repeating: cell, count: columns).joined(separator: "┼") + "┤"
        let row = "│" + (0..<columns).map { _ in String(format: "%10d ", 42) }.joined(separator: "│") + "│"
        let bottom = "└" + Array(repeating: cell, count: columns).joined(separator: "┴") + "┘"
        let lines = [top, names, middle, row, bottom]
        let text = lines.joined(separator: "\n")
        XCTAssertEqual(Set(lines.map(\.count)).count, 1, "the sample's lines are all as long")
        for size in sizes {
            let font = SQLConsoleScreen.font(for: size)
            let widths = lines.map { self.width($0, font) }
            for (line, w) in zip(lines, widths) {
                XCTAssertEqual(w, widths[0], accuracy: 0.5, "\(line.prefix(12))… at \(size)")
            }
            let view = ConsoleOutputScrollView()
            view.show(text: text, font: font, color: .label)
            view.frame = CGRect(x: 0, y: 0, width: 390, height: ConsoleOutputScrollView.height(of: text, font: font))
            view.layoutIfNeeded()
            XCTAssertGreaterThan(widths[0], 390, "wider than a phone at \(size)")
            XCTAssertGreaterThanOrEqual(view.textView.frame.width, widths[0], "the text view holds the longest line at \(size)")
            XCTAssertEqual(view.contentSize.width, view.textView.frame.width, "it scrolls sideways at \(size)")
            XCTAssertEqual(view.contentSize.height, view.bounds.height, "and only sideways at \(size)")
            let layout = view.textView.layoutManager
            layout.ensureLayout(for: view.textView.textContainer)
            var fragments = 0
            layout.enumerateLineFragments(forGlyphRange: layout.glyphRange(for: view.textView.textContainer)) { _, _, _, _, _ in
                fragments += 1
            }
            XCTAssertEqual(fragments, lines.count, "no line wrapped at \(size)")
            XCTAssertLessThanOrEqual(layout.usedRect(for: view.textView.textContainer).height,
                                     ConsoleOutputScrollView.height(of: text, font: font), "the view is as tall as its lines at \(size)")
        }
    }

    /// The widest line is found even when it isn't the one with the most characters: wide
    /// (CJK) characters take two cells each.
    func testTheWidestLineCountsWideCharacters() {
        let font = SQLConsoleScreen.font(for: .large)
        let lines = (0..<20).map { "│ \(String(repeating: "x", count: 30 + $0)) │" } + ["│ \(String(repeating: "表", count: 40)) │"]
        let widest = lines.map { self.width($0, font) }.max() ?? 0
        XCTAssertEqual(ConsoleOutputScrollView.width(of: lines.shuffled().joined(separator: "\n"), font: font),
                       ceil(widest) + 2, accuracy: 0.5)
    }

    /// Measuring a long result (2,000 lines at the largest text size) is quick, and its height
    /// is its lines times the line height.
    func testMeasuringALongResultIsQuick() {
        let font = SQLConsoleScreen.font(for: .accessibility5)
        let text = (0..<2_000).map { "│ \(String(repeating: "─", count: $0 % 300)) │" }.joined(separator: "\n")
        let started = Date()
        let width = ConsoleOutputScrollView.width(of: text, font: font)
        XCTAssertLessThan(Date().timeIntervalSince(started), 1, "measuring 2,000 lines is quick")
        XCTAssertGreaterThan(width, 5_000)
        XCTAssertEqual(ConsoleOutputScrollView.height(of: text, font: font), ceil(2_000 * font.lineHeight) + 2)
    }
}
