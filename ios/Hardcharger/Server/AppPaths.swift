import Foundation

/// Where the app keeps its files, inside its container.
///
/// `Documents` is the part people see in the Files app (UIFileSharingEnabled): the database,
/// the files they import, and what the console exports. The rest lives under `Library`,
/// which Files doesn't show: the server's TLS key and certificate, its log, and DuckDB's
/// spill directory.
struct AppPaths: Sendable, Equatable {
    /// The container's home (`NSHomeDirectory()`), or a test's own directory.
    let root: URL

    var documents: URL { root.appending(path: "Documents", directoryHint: .isDirectory) }
    var library: URL { root.appending(path: "Library", directoryHint: .isDirectory) }

    /// The DuckDB database the server opens.
    var database: URL { documents.appending(path: "Hardcharger.duckdb") }
    /// Files imported from Files, the share sheet or a drop, kept so tables can be re-read.
    var imports: URL { documents.appending(path: "Imports", directoryHint: .isDirectory) }
    /// Files the web console downloads (CSV, Parquet, Excel exports).
    var exports: URL { documents.appending(path: "Exports", directoryHint: .isDirectory) }

    /// The server's own files: its TLS key and certificate.
    var support: URL {
        library.appending(path: "Application Support/Hardcharger", directoryHint: .isDirectory)
    }
    var tlsCertificate: URL { support.appending(path: "tls-certificate.pem") }
    var tlsKey: URL { support.appending(path: "tls-key.pem") }

    var logs: URL { library.appending(path: "Logs", directoryHint: .isDirectory) }
    var logFile: URL { logs.appending(path: "hardcharger-server.log") }
    /// The previous log, kept when the current one grows past `LogRotation.limit`.
    var previousLogFile: URL { logs.appending(path: "hardcharger-server.1.log") }

    /// DuckDB's temp_directory (larger-than-memory work), out of the Files app's sight.
    var spill: URL { library.appending(path: "Caches/duckdb-spill", directoryHint: .isDirectory) }

    static let standard = AppPaths(root: URL(filePath: NSHomeDirectory(), directoryHint: .isDirectory))

    /// The parts of the container no SQL statement may read or write (the server's
    /// sensitive_paths, on top of its own built-in list): the app's preferences, the web
    /// views' storage and cookies, URL caches, and the server's own key. The database,
    /// imports and exports stay open to SQL; that is what they are for.
    var privatePaths: [String] {
        [
            library.appending(path: "Preferences").path(percentEncoded: false),
            library.appending(path: "WebKit").path(percentEncoded: false),
            library.appending(path: "Cookies").path(percentEncoded: false),
            library.appending(path: "HTTPStorages").path(percentEncoded: false),
            library.appending(path: "Caches/com.gizmodata.hardcharger").path(percentEncoded: false),
            library.appending(path: "SplashBoard").path(percentEncoded: false),
            support.path(percentEncoded: false),
        ]
    }

    func createDirectories() throws {
        let fm = FileManager.default
        for directory in [documents, imports, exports, support, logs, spill] {
            try fm.createDirectory(at: directory, withIntermediateDirectories: true)
        }
        // The key and certificate are written by the app only; keep them out of backups too.
        var support = self.support
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try? support.setResourceValues(values)
    }
}

/// Keeps the server's log from growing without bound: one previous file is kept.
enum LogRotation {
    static let limit = 8 * 1024 * 1024

    static func rotateIfNeeded(_ paths: AppPaths) {
        let fm = FileManager.default
        guard let size = try? fm.attributesOfItem(atPath: paths.logFile.path(percentEncoded: false))[.size] as? Int,
              size > limit
        else { return }
        try? fm.removeItem(at: paths.previousLogFile)
        try? fm.moveItem(at: paths.logFile, to: paths.previousLogFile)
    }
}
