import Foundation
import HardchargerServer
import Synchronization

/// The `hardcharger` command-line client inside the app (hardcharger_embedded.h's console):
/// the same statement splitting, dot commands and output formatting as the desktop client,
/// on a queue of its own (statements block while they run).
final class ConsoleEngine: Sendable {
    struct Options: Sendable {
        var host: String
        var port: Int
        var tls: Bool
        /// PEM file of the certificates to trust; nil: the system's (the app's CA bundle).
        var tlsRoots: String?
        var username: String?
        var password: String?
        var accessToken: String?
        var maxRows: Int = 1000
    }

    struct OpenFailure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    struct Output: Sendable {
        let text: String
        let exitRequested: Bool
    }

    private let handle = Mutex<UInt>(0)
    private let queue = DispatchQueue(label: "com.gizmodata.hardcharger.console", qos: .userInitiated)

    deinit {
        let address = handle.withLock { $0 }
        if address != 0 {
            queue.async { hardcharger_console_close(OpaquePointer(bitPattern: address)) }
        }
    }

    var isOpen: Bool { handle.withLock { $0 != 0 } }

    func open(_ options: Options) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            queue.async { [self] in
                closeNow()
                var error = [CChar](repeating: 0, count: 1024)
                var console: OpaquePointer?
                let status = Self.withOptions(options) { raw in
                    withUnsafePointer(to: raw) { hardcharger_console_open($0, &console, &error, error.count) }
                }
                if status == 0, let console {
                    handle.withLock { $0 = UInt(bitPattern: console) }
                    continuation.resume()
                } else {
                    continuation.resume(throwing: OpenFailure(message: String(cBuffer: error)))
                }
            }
        }
    }

    /// Runs input as the desktop client would (`hardcharger -c`), and returns what it printed.
    func run(_ input: String) async -> Output {
        await withCheckedContinuation { continuation in
            queue.async { [self] in
                guard let console = OpaquePointer(bitPattern: handle.withLock { $0 }) else {
                    continuation.resume(returning: Output(text: "Error: not connected\n", exitRequested: false))
                    return
                }
                var exit: Int32 = 0
                let text = hardcharger_console_run(console, input, &exit)
                let output = text.map { String(cString: $0) } ?? ""
                hardcharger_console_free_text(text)
                continuation.resume(returning: Output(text: output, exitRequested: exit != 0))
            }
        }
    }

    /// Cancels the running statement (safe while `run` waits on the queue).
    func cancel() {
        handle.withLock { address in
            if let console = OpaquePointer(bitPattern: address) { hardcharger_console_cancel(console) }
        }
    }

    func prompt(continuation: Bool) async -> String {
        await withCheckedContinuation { result in
            queue.async { [self] in
                let console = OpaquePointer(bitPattern: handle.withLock { $0 })
                let text = hardcharger_console_prompt(console, continuation ? 1 : 0)
                let prompt = text.map { String(cString: $0) } ?? "hardcharger> "
                hardcharger_console_free_text(text)
                result.resume(returning: prompt)
            }
        }
    }

    func close() {
        queue.async { [self] in closeNow() }
    }

    private func closeNow() {
        let address = handle.withLock { value -> UInt in
            defer { value = 0 }
            return value
        }
        if let console = OpaquePointer(bitPattern: address) { hardcharger_console_close(console) }
    }

    /// Whether input is ready to run as the desktop client reads it: every statement ends
    /// with ';' (outside strings, quoted names, comments, dollar quotes), or it is a dot
    /// command; and how many statements it holds.
    static func readiness(of input: String) -> (complete: Bool, statements: Int) {
        var statements: Int32 = 0
        let complete = hardcharger_console_input_complete(input, &statements) == 1
        return (complete, Int(statements))
    }

    private static func withOptions<T>(_ options: Options, _ body: (hardcharger_console_options) -> T) -> T {
        func cString(_ text: String?) -> UnsafeMutablePointer<CChar>? { text.map { strdup($0) } }
        let host = strdup(options.host)
        let roots = cString(options.tlsRoots)
        let user = cString(options.username)
        let password = cString(options.password)
        let token = cString(options.accessToken)
        defer { [host, roots, user, password, token].forEach { free($0) } }
        var raw = hardcharger_console_options()
        raw.host = UnsafePointer(host)
        raw.port = Int32(options.port)
        raw.tls = options.tls ? 1 : 0
        raw.tls_roots = UnsafePointer(roots)
        raw.username = UnsafePointer(user)
        raw.password = UnsafePointer(password)
        raw.access_token = UnsafePointer(token)
        raw.max_width = 0
        raw.max_rows = Int32(options.maxRows)
        return body(raw)
    }
}
