import Foundation
import UniformTypeIdentifiers

/// Files people bring in (Files, the share sheet, a drop on iPad) become tables in the
/// on-device database: the file is copied into Documents/Imports, then DuckDB reads it
/// there (`CREATE OR REPLACE TABLE … AS SELECT * FROM read_…`), through the server like any
/// other statement.
enum DataImport {
    enum Format: String, Sendable {
        case csv, json, parquet

        var reader: String {
            switch self {
            case .csv: "read_csv"
            case .json: "read_json_auto"
            case .parquet: "read_parquet"
            }
        }
    }

    struct Result: Sendable, Identifiable, Equatable {
        let id = UUID()
        let file: String
        let table: String
        let rows: Int?
    }

    struct Unsupported: LocalizedError {
        let name: String
        var errorDescription: String? {
            if name.lowercased().hasSuffix(".xlsx") || name.lowercased().hasSuffix(".xls") {
                return String(localized: "\(name) is an Excel workbook: open the console's Upload to load its sheets.")
            }
            return String(localized: "\(name) isn't a CSV, TSV, JSON or Parquet file.")
        }
    }

    static let contentTypes: [UTType] = [
        .commaSeparatedText, .tabSeparatedText, .json, .plainText, UTType("org.apache.parquet") ?? .data, .data,
    ]

    static func format(of url: URL) -> Format? {
        switch url.pathExtension.lowercased() {
        case "csv", "tsv", "tab", "txt", "psv": .csv
        case "json", "ndjson", "jsonl": .json
        case "parquet", "pq": .parquet
        default: nil
        }
    }

    /// A table name from a file name: letters, digits and underscores, not starting with a
    /// digit ("Sales 2026.csv" → sales_2026).
    static func tableName(for url: URL) -> String {
        let stem = url.deletingPathExtension().lastPathComponent
            .folding(options: [.diacriticInsensitive, .caseInsensitive, .widthInsensitive], locale: nil)
            .lowercased()
        var name = ""
        var lastWasUnderscore = false
        for scalar in stem.unicodeScalars {
            if CharacterSet.alphanumerics.contains(scalar), scalar.isASCII {
                name.unicodeScalars.append(scalar)
                lastWasUnderscore = false
            } else if !lastWasUnderscore, !name.isEmpty {
                name.append("_")
                lastWasUnderscore = true
            }
        }
        while name.hasSuffix("_") { name.removeLast() }
        if name.isEmpty { name = "imported" }
        if name.first?.isNumber == true { name = "t_" + name }
        return name
    }

    /// A double-quoted SQL identifier.
    static func quotedIdentifier(_ name: String) -> String {
        "\"" + name.replacingOccurrences(of: "\"", with: "\"\"") + "\""
    }

    static func statement(table: String, file: URL, format: Format) -> String {
        "CREATE OR REPLACE TABLE \(quotedIdentifier(table)) AS SELECT * FROM "
            + "\(format.reader)(\(ServerLaunch.quoted(file.path(percentEncoded: false))))"
    }

    /// Copies a file the person picked (security-scoped) into Imports, under a name no other
    /// import has.
    static func copyIntoImports(_ source: URL, paths: AppPaths) throws -> URL {
        let scoped = source.startAccessingSecurityScopedResource()
        defer { if scoped { source.stopAccessingSecurityScopedResource() } }
        try FileManager.default.createDirectory(at: paths.imports, withIntermediateDirectories: true)
        // A file already in Imports (opened in place) is used where it is.
        if source.standardizedFileURL.deletingLastPathComponent().path == paths.imports.standardizedFileURL.path {
            return source
        }
        var destination = paths.imports.appending(path: source.lastPathComponent)
        var n = 2
        while FileManager.default.fileExists(atPath: destination.path(percentEncoded: false)) {
            let base = source.deletingPathExtension().lastPathComponent
            destination = paths.imports.appending(path: "\(base) \(n).\(source.pathExtension)")
            n += 1
        }
        let coordinator = NSFileCoordinator()
        var coordinationError: NSError?
        var copyError: Error?
        coordinator.coordinate(readingItemAt: source, options: .forUploading, error: &coordinationError) { readable in
            do { try FileManager.default.copyItem(at: readable, to: destination) } catch { copyError = error }
        }
        if let error = coordinationError ?? copyError { throw error }
        return destination
    }

    /// Imports one file into a table; returns what it made.
    @MainActor
    static func run(_ source: URL, server: ServerController) async throws -> Result {
        guard let format = format(of: source) else { throw Unsupported(name: source.lastPathComponent) }
        guard let endpoint = server.localEndpoint else {
            throw WarpError(status: 0, code: "client_not_running", detail: String(localized: "Start the server first."))
        }
        let paths = server.paths
        let file = try await Task.detached { try copyIntoImports(source, paths: paths) }.value
        let table = tableName(for: source)
        let token = try await server.accessToken()
        let rows = try await WarpClient(endpoint: endpoint, timeout: 330)
            .execute(statement(table: table, file: file, format: format), token: token)
        return Result(file: source.lastPathComponent, table: table, rows: rows)
    }
}
