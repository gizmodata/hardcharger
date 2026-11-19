import SwiftUI
import UIKit

/// One result's text in the console: monospaced, never wrapped (tables keep their shape and
/// each result scrolls sideways on its own), selectable. A UITextView, since a result can be thousands of lines
/// (`.tables` on a real schema, a large `.maxrows`), which TextKit lays out far faster than
/// a selectable SwiftUI Text.
///
/// The sideways scrolling is UIKit's (ConsoleOutputScrollView), not a SwiftUI ScrollView, so to
/// the transcript this view's size is a plain function of its text: as wide as offered, as tall
/// as its lines.
struct ConsoleOutputView: UIViewRepresentable {
    let text: String
    let font: UIFont
    let color: UIColor

    func makeUIView(context: Context) -> ConsoleOutputScrollView { ConsoleOutputScrollView() }

    func updateUIView(_ view: ConsoleOutputScrollView, context: Context) {
        view.show(text: text, font: font, color: color)
    }

    func sizeThatFits(_ proposal: ProposedViewSize, uiView: ConsoleOutputScrollView, context: Context) -> CGSize? {
        // As tall as its lines; as wide as offered (it scrolls sideways within that).
        CGSize(width: proposal.width.flatMap { $0.isFinite ? $0 : nil } ?? 320,
               height: ConsoleOutputScrollView.height(of: text, font: font))
    }
}

/// A horizontal scroll view around a text view as wide as its text's longest line, so no line
/// ever wraps: a table wider than the screen keeps its borders and scrolls sideways.
final class ConsoleOutputScrollView: UIScrollView {
    let textView: UITextView
    /// The text's longest line in its font (measured once per text and font).
    private(set) var textWidth: CGFloat = 0

    override init(frame: CGRect) {
        // TextKit 1: its text container keeps the fixed width set below, so a line wraps only
        // at its own newline (TextKit 2 wrapped long table borders at the view's width).
        textView = UITextView(usingTextLayoutManager: false)
        super.init(frame: frame)
        textView.isEditable = false
        textView.isSelectable = true
        // The text view is laid out at its full size here and this scroll view does the
        // scrolling, so the text view's own pan is off. Its scrolling stays enabled all the same:
        // a UITextView without it reports no frame to accessibility (VoiceOver, UI tests).
        textView.isScrollEnabled = true
        textView.panGestureRecognizer.isEnabled = false
        textView.alwaysBounceVertical = false
        textView.alwaysBounceHorizontal = false
        textView.showsVerticalScrollIndicator = false
        textView.showsHorizontalScrollIndicator = false
        textView.scrollsToTop = false
        textView.backgroundColor = .clear
        textView.textContainerInset = .zero
        textView.textContainer.lineFragmentPadding = 0
        textView.textContainer.widthTracksTextView = false
        textView.textContainer.heightTracksTextView = false
        textView.textContainer.size = CGSize(width: 1_000_000, height: CGFloat.greatestFiniteMagnitude)
        textView.dataDetectorTypes = []
        textView.accessibilityIdentifier = "sqlconsole.output"
        addSubview(textView)
        backgroundColor = .clear
        alwaysBounceVertical = false
        alwaysBounceHorizontal = false
        isDirectionalLockEnabled = true
        showsVerticalScrollIndicator = false
        // Only sideways: a vertical drag that starts on a result scrolls the transcript.
        contentInsetAdjustmentBehavior = .never
        accessibilityIdentifier = "sqlconsole.outputScroll"
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    func show(text: String, font: UIFont, color: UIColor) {
        guard textView.text != text || textView.font != font || textView.textColor != color else { return }
        textView.text = text
        textView.font = font
        textView.textColor = color
        textView.accessibilityValue = text
        textWidth = Self.width(of: text, font: font)
        setNeedsLayout()
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        let size = CGSize(width: max(bounds.width, textWidth), height: bounds.height)
        if textView.frame.size != size { textView.frame = CGRect(origin: .zero, size: size) }
        if contentSize != size { contentSize = size }
    }

    /// The height of the text's lines in `font` (one line at least).
    static func height(of text: String, font: UIFont) -> CGFloat {
        // Newlines counted in UTF-8 (a byte scan), not Characters: it runs on every layout pass.
        let lines = text.utf8.reduce(into: 1) { n, byte in if byte == 0x0A { n += 1 } }
        return ceil(CGFloat(lines) * font.lineHeight) + 2
    }

    /// The width of the text's longest line in `font`. Measuring every line of a long result
    /// would cost more than the layout; the widest line is among the few with the most
    /// characters, or the most UTF-8 bytes (wide characters, such as CJK, take two cells and
    /// three bytes), so those are measured.
    static func width(of text: String, font: UIFont) -> CGFloat {
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
        var candidates = Array(lines.sorted { $0.count > $1.count }.prefix(8))
        candidates += lines.sorted { $0.utf8.count > $1.utf8.count }.prefix(8)
        let widest = candidates.map { (String($0) as NSString).size(withAttributes: [.font: font]).width }.max() ?? 0
        return ceil(widest) + 2
    }
}
