import Foundation

/// What a person can change about the on-device server (Settings). Kept in UserDefaults;
/// nothing here is secret (the credential is in the Keychain).
struct ServerSettings: Codable, Equatable, Sendable {
    /// The port the server listens on.
    var port: Int = 31337
    /// DuckDB's memory_limit.
    var memoryLimit: MemoryLimit = .automatic
    /// DuckDB's worker threads; 0: one per core.
    var threads: Int = 0
    var logLevel: ServerLogLevel = .info
    /// Log every statement (print_queries).
    var printQueries: Bool = true
    /// Start the server when the app opens.
    var startAutomatically: Bool = true
    /// Serve on the local network (all interfaces, TLS on). Off by default.
    var shareOnLocalNetwork: Bool = false
    /// TLS on this device's own connections too (sharing always uses it).
    var alwaysUseTLS: Bool = false
    /// Keep the screen on while sharing, so iOS doesn't suspend the server.
    var keepAwakeWhileSharing: Bool = true
    /// How the log view marks levels.
    var logStyle: LogStyle = .emoji

    /// Whether the server runs with TLS.
    var usesTLS: Bool { shareOnLocalNetwork || alwaysUseTLS }

    static let validPorts = 1024...65535

    private static let key = "serverSettings.v1"

    static func load(from defaults: UserDefaults = .standard) -> ServerSettings {
        guard let data = defaults.data(forKey: key),
              let settings = try? JSONDecoder().decode(ServerSettings.self, from: data)
        else { return ServerSettings() }
        return settings
    }

    func save(to defaults: UserDefaults = .standard) {
        if let data = try? JSONEncoder().encode(self) {
            defaults.set(data, forKey: Self.key)
        }
    }

    /// Settings that only take effect when the server starts again.
    func needsRestart(comparedTo running: ServerSettings) -> Bool {
        var a = self, b = running
        // These the app applies by itself.
        for s in [\ServerSettings.startAutomatically, \ServerSettings.keepAwakeWhileSharing] {
            a[keyPath: s] = false
            b[keyPath: s] = false
        }
        a.logStyle = .plain
        b.logStyle = .plain
        return a != b
    }
}

enum MemoryLimit: String, Codable, CaseIterable, Identifiable, Sendable {
    case automatic, mb512, gb1, gb2, gb4, gb8

    var id: String { rawValue }

    /// The value for DuckDB, in megabytes. Automatic: a share of the device's memory that
    /// leaves the app well inside what iOS lets a foreground app use (iOS ends an app that
    /// uses too much, where a server would only spill to disk).
    func megabytes(physicalMemory: UInt64 = ProcessInfo.processInfo.physicalMemory) -> Int {
        switch self {
        case .automatic:
            let physical = Int(physicalMemory / (1024 * 1024))
            return max(256, min(physical * 30 / 100, 4096))
        case .mb512: return 512
        case .gb1: return 1024
        case .gb2: return 2048
        case .gb4: return 4096
        case .gb8: return 8192
        }
    }

    var label: LocalizedStringResource {
        switch self {
        case .automatic: "Automatic"
        case .mb512: "512 MB"
        case .gb1: "1 GB"
        case .gb2: "2 GB"
        case .gb4: "4 GB"
        case .gb8: "8 GB"
        }
    }
}

enum ServerLogLevel: String, Codable, CaseIterable, Identifiable, Sendable {
    case debug, info, warning, error
    var id: String { rawValue }
    var label: LocalizedStringResource {
        switch self {
        case .debug: "Debug"
        case .info: "Info"
        case .warning: "Warning"
        case .error: "Error"
        }
    }
}

/// The server log, as the app shows it: with the level marks Hardcharger's log_emoji uses
/// (⚠️ warning, ❌ error, ✅ ready and stopped, 💥 the error that stops it), or plain text.
enum LogStyle: String, Codable, CaseIterable, Identifiable, Sendable {
    case emoji, plain
    var id: String { rawValue }
    var label: LocalizedStringResource {
        switch self {
        case .emoji: "Emoji"
        case .plain: "Plain"
        }
    }
}
