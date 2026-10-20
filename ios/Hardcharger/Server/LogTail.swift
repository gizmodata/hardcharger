import Foundation
import Observation

/// One line of the server's text log: `<time> <level> <message> key=value…`
/// (server/src/logging.cc), possibly with the level mark log_emoji adds before the level.
struct LogLine: Identifiable, Equatable, Sendable {
    enum Level: String, CaseIterable, Sendable, Comparable {
        case debug, info, warning, error

        static func < (a: Level, b: Level) -> Bool { a.order < b.order }
        private var order: Int { Self.allCases.firstIndex(of: self)! }
    }

    /// What a line reports, for its mark: a success (the server is ready, or stopped), the
    /// error that stops it, or just its level.
    enum Outcome: Sendable { case level, success, fatal }

    let id: Int
    let raw: String
    let time: String
    let level: Level?
    let message: String

    var outcome: Outcome {
        if level == .info, message.hasPrefix("Hardcharger is ready") || message == "stopped" { return .success }
        return .level
    }

    /// The marks Hardcharger's log_emoji uses (server/src/logging.cc: ⚠️ ❌ ✅ 💥), so the app
    /// shows the same log either way.
    var emoji: String {
        if outcome == .success { return "✅" }
        switch level {
        case .warning: return "⚠️"
        case .error: return "❌"
        default: return ""
        }
    }

    static let marks: Set<String> = ["✅", "💥", "⚠️", "❌"]

    static func parse(_ raw: String, id: Int) -> LogLine {
        var parts = raw.split(separator: " ", maxSplits: 1, omittingEmptySubsequences: true).map(String.init)
        guard parts.count == 2, parts[0].count >= 20, parts[0].hasSuffix("Z") else {
            return LogLine(id: id, raw: raw, time: "", level: nil, message: raw)
        }
        let time = parts[0]
        var rest = parts[1].drop { $0 == " " }
        // A level mark (log_emoji) before the level.
        if let first = rest.split(separator: " ", maxSplits: 1).first, marks.contains(String(first)) {
            rest = rest.dropFirst(first.count).drop { $0 == " " }
        }
        parts = rest.split(separator: " ", maxSplits: 1, omittingEmptySubsequences: true).map(String.init)
        guard let word = parts.first, let level = Level(rawValue: word) else {
            return LogLine(id: id, raw: raw, time: time, level: nil, message: String(rest))
        }
        let message = parts.count > 1 ? parts[1].trimmingCharacters(in: .whitespaces) : ""
        return LogLine(id: id, raw: raw, time: time, level: level, message: message)
    }

    /// This entry with a continuation line added (a logged statement's next line).
    func continued(with text: String) -> LogLine {
        LogLine(id: id, raw: raw + "\n" + text, time: time, level: level, message: message + "\n" + text)
    }

    /// The line as shown: with marks, or plain (marks the server wrote taken out).
    func text(style: LogStyle) -> String {
        guard let level else { return raw }
        let stamp = Self.shortTime(time)
        switch style {
        case .emoji:
            let mark = emoji.isEmpty ? "  " : emoji
            return "\(stamp) \(mark) \(level.rawValue.padding(toLength: 7, withPad: " ", startingAt: 0)) \(message)"
        case .plain:
            return "\(stamp) \(level.rawValue.padding(toLength: 7, withPad: " ", startingAt: 0)) \(message)"
        }
    }

    /// 2026-10-20T10:46:09.923Z → 10:46:09.923
    static func shortTime(_ time: String) -> String {
        guard let t = time.firstIndex(of: "T") else { return time }
        return String(time[time.index(after: t)...].dropLast())
    }
}

/// Follows the server's log file while the log view is open: reads what was appended, keeps
/// the last `limit` lines, and starts over when the file is rotated or truncated.
@MainActor
@Observable
final class LogTail {
    private(set) var lines: [LogLine] = []
    let file: URL
    let limit: Int

    @ObservationIgnored private var offset: UInt64 = 0
    @ObservationIgnored private var partial = ""
    @ObservationIgnored private var nextID = 0
    @ObservationIgnored private var task: Task<Void, Never>?

    init(file: URL, limit: Int = 5000) {
        self.file = file
        self.limit = limit
    }

    func start() {
        guard task == nil else { return }
        // The last 256 KB to begin with.
        if let size = try? FileManager.default.attributesOfItem(atPath: file.path(percentEncoded: false))[.size]
            as? NSNumber
        {
            offset = UInt64(max(0, size.int64Value - 256 * 1024))
        }
        task = Task { [weak self] in
            while !Task.isCancelled {
                self?.poll()
                try? await Task.sleep(for: .milliseconds(400))
            }
        }
    }

    func stop() {
        task?.cancel()
        task = nil
    }

    /// Empties the view (the file stays: the server keeps writing it).
    func clear() {
        lines = []
    }

    func poll() {
        guard let handle = try? FileHandle(forReadingFrom: file) else { return }
        defer { try? handle.close() }
        let end = (try? handle.seekToEnd()) ?? 0
        if end < offset {  // rotated or truncated
            offset = 0
            partial = ""
        }
        guard end > offset else { return }
        try? handle.seek(toOffset: offset)
        guard let data = try? handle.read(upToCount: Int(min(end - offset, 4 * 1024 * 1024))) else { return }
        offset += UInt64(data.count)
        let text = partial + String(decoding: data, as: UTF8.self)
        var pieces = text.components(separatedBy: "\n")
        partial = pieces.removeLast()
        guard !pieces.isEmpty else { return }
        var fresh: [LogLine] = []
        fresh.reserveCapacity(pieces.count)
        for piece in pieces where !piece.isEmpty {
            let line = LogLine.parse(piece, id: nextID)
            // A line without a time and level continues the entry before it (a statement's
            // text with line breaks).
            if line.level == nil {
                if let last = fresh.last {
                    fresh[fresh.count - 1] = last.continued(with: piece)
                    continue
                }
                if let last = lines.last {
                    lines[lines.count - 1] = last.continued(with: piece)
                    continue
                }
            }
            fresh.append(line)
            nextID += 1
        }
        lines.append(contentsOf: fresh)
        if lines.count > limit { lines.removeFirst(lines.count - limit) }
    }
}
