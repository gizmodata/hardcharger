import Foundation

/// How the app starts hardcharger-server in its own process: the same command line as
/// anywhere else (`--name=value` flags), plus the two secrets in its environment
/// (HARDCHARGER_ADMIN_PASSWORD, HARDCHARGER_SECRET_KEY), which the server reads at start
/// like any other option. Kept out of the flags because the server's log says a password
/// flag is visible in process listings.
struct ServerLaunch: Sendable, Equatable {
    var arguments: [String]
    var environment: [String: String]

    /// The address the server listens on.
    static let loopback = "127.0.0.1"
    static let allInterfaces = "0.0.0.0"

    init(settings: ServerSettings, paths: AppPaths, credential: LocalCredential,
         physicalMemory: UInt64 = ProcessInfo.processInfo.physicalMemory) {
        var args = [
            "hardcharger-server",
            "--host=\(settings.shareOnLocalNetwork ? Self.allInterfaces : Self.loopback)",
            "--port=\(settings.port)",
            "--database=\(paths.database.path(percentEncoded: false))",
            "--web-ui=true",
            "--admin-username=\(credential.username)",
            "--log-file=\(paths.logFile.path(percentEncoded: false))",
            "--log-level=\(settings.logLevel.rawValue)",
            "--print-queries=\(settings.printQueries)",
            "--memory-limit=\(settings.memoryLimit.megabytes(physicalMemory: physicalMemory))MB",
            // A stop drains running statements for a few seconds, no longer: iOS gives an app
            // going away little time.
            "--graceful-shutdown=true",
            "--shutdown-grace-period-seconds=5",
            "--sensitive-paths=\(paths.privatePaths.joined(separator: ","))",
            "--init-sql-commands=\(Self.initSQL(settings: settings, paths: paths))",
        ]
        if settings.usesTLS {
            args.append("--tls-cert=\(paths.tlsCertificate.path(percentEncoded: false))")
            args.append("--tls-key=\(paths.tlsKey.path(percentEncoded: false))")
        }
        arguments = args
        environment = [
            "HARDCHARGER_ADMIN_PASSWORD": credential.password,
            "HARDCHARGER_SECRET_KEY": credential.secretKey,
        ]
    }

    /// DuckDB settings the server has no option of its own for: where it spills (out of
    /// the Files app's sight) and its thread count.
    static func initSQL(settings: ServerSettings, paths: AppPaths) -> String {
        var sql = "SET GLOBAL temp_directory = \(quoted(paths.spill.path(percentEncoded: false)));"
        if settings.threads > 0 {
            sql += " SET GLOBAL threads = \(settings.threads);"
        }
        return sql
    }

    /// A SQL string literal.
    static func quoted(_ text: String) -> String {
        "'" + text.replacingOccurrences(of: "'", with: "''") + "'"
    }

    /// The arguments with nothing secret in them, for a log or a test's message.
    var redactedDescription: String {
        arguments.joined(separator: " ")
    }
}
