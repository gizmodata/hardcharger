import Foundation
import Observation

/// One SQL console: a transcript of what was run and what it printed, the input being
/// written, and its history. It talks to its server through the `hardcharger` client
/// (ConsoleEngine), so statements end, dot commands work and results look exactly as in
/// the desktop client.
///
/// Reading input: Return adds a line; the input runs once it is complete as the client reads
/// it (each statement ended by a ';' outside strings, quoted names, comments and dollar
/// quotes, or a dot command first on its line). Run (⌘↩) runs it as it stands, a last
/// statement without its ';' included.
@MainActor
@Observable
final class SQLConsole {
    struct Entry: Identifiable, Equatable {
        enum Kind: Equatable { case run, notice, error }
        let id = UUID()
        let prompt: String
        let input: String
        var output: String
        var kind: Kind
        var duration: Duration?
    }

    let target: ConsoleTarget
    private(set) var entries: [Entry] = []
    var input = ""
    private(set) var prompt = "hardcharger> "
    private(set) var continuationPrompt = "          -> "
    private(set) var isRunning = false
    private(set) var isConnecting = false
    private(set) var connectionError: String?
    private(set) var isConnected = false
    /// The connection this console last opened with, to reopen after the server restarted.
    @ObservationIgnored private var connectedFor: String?

    @ObservationIgnored private let engine = ConsoleEngine()
    @ObservationIgnored private var history: [String]
    @ObservationIgnored private var historyIndex: Int?
    @ObservationIgnored private var draft = ""

    static let historyKey = "sqlConsole.history.v1"
    static let historyLimit = 500

    init(target: ConsoleTarget) {
        self.target = target
        history = UserDefaults.standard.stringArray(forKey: Self.historyKey) ?? []
    }

    // MARK: - Connecting

    /// Connects (again) when `key` (the server's identity: address, start time) changed.
    func connect(options: @escaping () async throws -> ConsoleEngine.Options, key: String) async {
        guard connectedFor != key || !isConnected else { return }
        isConnecting = true
        connectionError = nil
        defer { isConnecting = false }
        do {
            try await engine.open(try await options())
            connectedFor = key
            isConnected = true
            prompt = await engine.prompt(continuation: false)
            continuationPrompt = await engine.prompt(continuation: true)
        } catch {
            isConnected = false
            connectionError = error.localizedDescription
        }
    }

    func disconnect() {
        engine.close()
        isConnected = false
        connectedFor = nil
    }

    // MARK: - Input

    /// Whether the input runs when Return is pressed now (rather than adding a line).
    var inputIsComplete: Bool { ConsoleEngine.readiness(of: input).complete }

    /// The prompt for the input's current line: the main prompt, or the continuation while
    /// a statement is open.
    func promptForLine(_ index: Int) -> String { index == 0 ? prompt : continuationPrompt }

    /// Return was pressed: run when the input is complete. Returns true when it ran.
    @discardableResult
    func returnPressed() -> Bool {
        guard ConsoleEngine.readiness(of: input).complete else { return false }
        submit()
        return true
    }

    /// Runs the input as it stands (Run, ⌘↩).
    func submit() {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isRunning else { return }
        remember(text)
        input = ""
        if handleAppCommand(text) { return }
        Task { await run(text) }
    }

    func clearInput() {
        input = ""
        historyIndex = nil
    }

    func clearTranscript() {
        entries = []
    }

    func cancel() {
        engine.cancel()
    }

    private func run(_ text: String) async {
        guard isConnected else {
            entries.append(Entry(prompt: prompt, input: text, output: connectionError ?? "Not connected.", kind: .error))
            return
        }
        isRunning = true
        let entryPrompt = prompt
        var entry = Entry(prompt: entryPrompt, input: text, output: "", kind: .run)
        entries.append(entry)
        let started = ContinuousClock.now
        let output = await engine.run(text)
        entry.duration = ContinuousClock.now - started
        entry.output = output.text
        entry.kind = Self.looksLikeError(output.text) ? .error : .run
        if let index = entries.lastIndex(where: { $0.id == entry.id }) { entries[index] = entry }
        isRunning = false
        prompt = await engine.prompt(continuation: false)
        continuationPrompt = await engine.prompt(continuation: true)
        if output.exitRequested { clearTranscript() }
    }

    static func looksLikeError(_ output: String) -> Bool {
        output.split(separator: "\n").contains { $0.hasPrefix("Error") || $0 == "Query cancelled" }
    }

    /// Dot commands the app answers itself, where the desktop client's meaning doesn't fit
    /// an app: .exit/.quit clear the screen, .clear too, .history lists it.
    private func handleAppCommand(_ text: String) -> Bool {
        guard text.hasPrefix("."), !text.contains("\n") else { return false }
        let words = text.split(separator: " ").map(String.init)
        switch words.first {
        case ".exit", ".quit", ".clear":
            clearTranscript()
            return true
        case ".history":
            let list = history.suffix(50).enumerated().map { "\($0.offset + 1)  \($0.element)" }.joined(separator: "\n")
            entries.append(Entry(prompt: prompt, input: text, output: list + "\n", kind: .notice))
            return true
        default:
            return false
        }
    }

    // MARK: - History

    private func remember(_ text: String) {
        if history.last != text { history.append(text) }
        if history.count > Self.historyLimit { history.removeFirst(history.count - Self.historyLimit) }
        UserDefaults.standard.set(history, forKey: Self.historyKey)
        historyIndex = nil
    }

    /// The previous input (↑). Returns false when there is none.
    @discardableResult
    func historyBack() -> Bool {
        guard !history.isEmpty else { return false }
        if historyIndex == nil { draft = input }
        let index = max(0, (historyIndex ?? history.count) - 1)
        historyIndex = index
        input = history[index]
        return true
    }

    /// The next input (↓), back to what was being written.
    @discardableResult
    func historyForward() -> Bool {
        guard let index = historyIndex else { return false }
        if index + 1 < history.count {
            historyIndex = index + 1
            input = history[index + 1]
        } else {
            historyIndex = nil
            input = draft
        }
        return true
    }

    /// The transcript as text (copy, share).
    var transcript: String {
        entries.map { entry in
            let lines = entry.input.split(separator: "\n", omittingEmptySubsequences: false)
            let shown = lines.enumerated().map { ($0.offset == 0 ? entry.prompt : continuationPrompt) + $0.element }
            return shown.joined(separator: "\n") + "\n" + entry.output
        }.joined(separator: "\n")
    }
}
