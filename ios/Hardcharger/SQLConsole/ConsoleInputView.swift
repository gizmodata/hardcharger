import SwiftUI
import UIKit

/// The console's input: a monospaced editor that doesn't wrap (each line of SQL is one line,
/// beside its prompt), with no smart quotes or autocorrection (they would change the SQL),
/// and the desktop client's keys:
///
///   Return   runs complete input, otherwise starts a new line
///   ⌘Return  runs the input as it stands
///   ↑ / ↓    history, from the first / last line (elsewhere they move the cursor)
///   Esc      clears the input
///   ⌘.       cancels the running statement;  ⌘K clears the transcript
struct ConsoleInputView: UIViewRepresentable {
    @Binding var text: String
    let font: UIFont
    /// False while another tab shows: the input gives up the keyboard.
    var active: Bool = true
    var onReturn: () -> Bool
    var onRun: () -> Void
    var onHistoryBack: () -> Bool
    var onHistoryForward: () -> Bool
    var onEscape: () -> Void
    var onCancel: () -> Void
    var onClearTranscript: () -> Void

    func makeUIView(context: Context) -> ConsoleTextView {
        let view = ConsoleTextView()
        view.delegate = context.coordinator
        view.font = font
        view.backgroundColor = .clear
        view.autocorrectionType = .no
        view.autocapitalizationType = .none
        view.spellCheckingType = .no
        view.smartQuotesType = .no
        view.smartDashesType = .no
        view.smartInsertDeleteType = .no
        view.inlinePredictionType = .no
        view.keyboardType = .asciiCapable
        view.textContainerInset = .zero
        view.textContainer.lineFragmentPadding = 0
        // No wrapping: long lines scroll sideways, so each line stays beside its prompt.
        view.textContainer.widthTracksTextView = false
        view.textContainer.size = CGSize(width: 100_000, height: CGFloat.greatestFiniteMagnitude)
        view.alwaysBounceVertical = false
        view.showsHorizontalScrollIndicator = false
        view.accessibilityIdentifier = "sqlconsole.input"
        view.accessibilityLabel = String(localized: "SQL input")
        view.text = text
        return view
    }

    func updateUIView(_ view: ConsoleTextView, context: Context) {
        context.coordinator.parent = self
        view.actions = self
        if view.text != text {
            view.text = text
            view.selectedRange = NSRange(location: (text as NSString).length, length: 0)
        }
        if view.font != font { view.font = font }
        if !active, view.isFirstResponder { view.resignFirstResponder() }
    }

    func sizeThatFits(_ proposal: ProposedViewSize, uiView: ConsoleTextView, context: Context) -> CGSize? {
        let lines = max(1, text.reduce(into: 1) { count, c in if c == "\n" { count += 1 } })
        let height = CGFloat(min(lines, 10)) * font.lineHeight
        return CGSize(width: proposal.width ?? 300, height: ceil(height))
    }

    func makeCoordinator() -> Coordinator { Coordinator(parent: self) }

    final class Coordinator: NSObject, UITextViewDelegate {
        var parent: ConsoleInputView

        init(parent: ConsoleInputView) { self.parent = parent }

        func textViewDidChange(_ textView: UITextView) {
            parent.text = textView.text
        }

        func textView(_ textView: UITextView, shouldChangeTextIn range: NSRange, replacementText text: String) -> Bool {
            guard text == "\n" else { return true }
            // Return at the end of the input runs it when it is complete; elsewhere (or
            // while a statement is open) it is a new line.
            let atEnd = range.location >= (textView.text as NSString).length
            if atEnd, parent.onReturn() { return false }
            return true
        }
    }
}

final class ConsoleTextView: UITextView {
    var actions: ConsoleInputView?

    override var keyCommands: [UIKeyCommand]? {
        let run = UIKeyCommand(title: String(localized: "Run"), action: #selector(runCommand), input: "\r",
                               modifierFlags: .command)
        let up = UIKeyCommand(input: UIKeyCommand.inputUpArrow, modifierFlags: [], action: #selector(upCommand))
        let down = UIKeyCommand(input: UIKeyCommand.inputDownArrow, modifierFlags: [], action: #selector(downCommand))
        let escape = UIKeyCommand(input: UIKeyCommand.inputEscape, modifierFlags: [], action: #selector(escapeCommand))
        let cancel = UIKeyCommand(title: String(localized: "Cancel Statement"), action: #selector(cancelCommand),
                                  input: ".", modifierFlags: .command)
        let clear = UIKeyCommand(title: String(localized: "Clear Transcript"), action: #selector(clearCommand),
                                 input: "k", modifierFlags: .command)
        for command in [run, up, down, escape, cancel, clear] { command.wantsPriorityOverSystemBehavior = true }
        return [run, up, down, escape, cancel, clear]
    }

    @objc private func runCommand() { actions?.onRun() }
    @objc private func escapeCommand() { actions?.onEscape() }
    @objc private func cancelCommand() { actions?.onCancel() }
    @objc private func clearCommand() { actions?.onClearTranscript() }

    @objc private func upCommand() {
        if isCaretOnFirstLine, actions?.onHistoryBack() == true { return }
        moveCaret(.up)
    }

    @objc private func downCommand() {
        if isCaretOnLastLine, actions?.onHistoryForward() == true { return }
        moveCaret(.down)
    }

    private var caret: Int { selectedRange.location }
    private var isCaretOnFirstLine: Bool { !(text as NSString).substring(to: min(caret, (text as NSString).length)).contains("\n") }
    private var isCaretOnLastLine: Bool {
        let ns = text as NSString
        return !ns.substring(from: min(caret, ns.length)).contains("\n")
    }

    private func moveCaret(_ direction: UITextLayoutDirection) {
        guard let range = selectedTextRange,
              let position = position(from: range.end, in: direction, offset: 1)
        else { return }
        selectedTextRange = textRange(from: position, to: position)
    }
}
