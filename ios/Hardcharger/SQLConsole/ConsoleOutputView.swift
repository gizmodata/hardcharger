import SwiftUI
import UIKit

/// One result's text in the console: monospaced, never wrapped (tables keep their shape and
/// each result scrolls sideways on its own), selectable. A UITextView, since a result can be thousands of lines
/// (`.tables` on a real schema, a large `.maxrows`), which TextKit lays out far faster than
/// a selectable SwiftUI Text.
struct ConsoleOutputView: UIViewRepresentable {
    let text: String
    let font: UIFont
    let color: UIColor

    func makeUIView(context: Context) -> UITextView {
        // TextKit 1: its text container keeps the fixed width set below, so a line wraps only
        // at its own newline (TextKit 2 wrapped long table borders at the view's width).
        let view = UITextView(usingTextLayoutManager: false)
        view.isEditable = false
        view.isSelectable = true
        view.isScrollEnabled = true
        view.alwaysBounceVertical = false
        view.showsVerticalScrollIndicator = false
        view.backgroundColor = .clear
        view.textContainerInset = .zero
        view.textContainer.lineFragmentPadding = 0
        view.textContainer.widthTracksTextView = false
        view.textContainer.size = CGSize(width: 1_000_000, height: CGFloat.greatestFiniteMagnitude)
        view.dataDetectorTypes = []
        view.accessibilityIdentifier = "sqlconsole.output"
        return view
    }

    func updateUIView(_ view: UITextView, context: Context) {
        if view.text != text || view.font != font || view.textColor != color {
            view.text = text
            view.font = font
            view.textColor = color
            view.accessibilityValue = text
        }
    }

    func sizeThatFits(_ proposal: ProposedViewSize, uiView: UITextView, context: Context) -> CGSize? {
        // As tall as its lines; as wide as offered (it scrolls sideways within that).
        let lines = max(1, text.reduce(into: 1) { n, c in if c == "\n" { n += 1 } })
        return CGSize(width: proposal.width ?? 320, height: ceil(CGFloat(lines) * font.lineHeight) + 2)
    }
}
