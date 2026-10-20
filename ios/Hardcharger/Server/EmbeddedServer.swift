import Foundation
import HardchargerServer
import Synchronization

/// The Hardcharger server in this process: the C API of the hardcharger_embedded library
/// (server/embedded/hardcharger_embedded.h), behind async calls.
///
/// Starting blocks while DuckDB opens the database, and a server is watched by a thread that
/// waits for it to stop, so both run on threads of their own, never on the main thread or
/// Swift's cooperative pool. One server runs at a time in a process (the library refuses a
/// second), so the app has one of these.
final class EmbeddedServer: Sendable {
    struct Versions: Sendable, Equatable {
        let hardcharger: String
        let duckdb: String
        let warpSQL: String
        let webUI: String
    }

    struct StartFailure: LocalizedError, Sendable {
        let status: Int32
        let message: String
        var errorDescription: String? { message }
    }

    /// Why a server stopped: 0 and no message for a clean stop.
    struct Exit: Sendable, Equatable {
        let status: Int32
        let message: String?
    }

    static let versions = Versions(
        hardcharger: String(cString: hardcharger_embedded_version()),
        duckdb: String(cString: hardcharger_embedded_duckdb_version()),
        warpSQL: String(cString: hardcharger_embedded_warp_sql_version()),
        webUI: String(cString: hardcharger_embedded_web_ui_version()))

    /// THIRD_PARTY_NOTICES.md, as built into the server.
    static var thirdPartyNotices: String { String(cString: hardcharger_embedded_third_party_notices()) }

    /// The running server's handle. Only ever used while held here: the watcher takes it out
    /// before it frees it, so a stop request never meets a freed server.
    /// (Held as its address: a pointer isn't Sendable.)
    private let handle = Mutex<UInt>(0)
    /// Starts and stops one at a time.
    private let queue = DispatchQueue(label: "com.gizmodata.hardcharger.server", qos: .userInitiated)

    var isRunning: Bool { handle.withLock { $0 != 0 } }

    /// Starts a server and returns the port it listens on once it serves. `onExit` is called
    /// (on a background thread) when that server has stopped, however it stopped.
    func start(launch: ServerLaunch, onExit: @escaping @Sendable (Exit) -> Void) async throws -> Int {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Int, Error>) in
            queue.async { [self] in
                do {
                    let (server, port) = try Self.startServer(launch)
                    let address = UInt(bitPattern: server)
                    handle.withLock { $0 = address }
                    watch(address, onExit: onExit)
                    continuation.resume(returning: port)
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    /// Asks the server to stop (draining running statements briefly, or at once) and
    /// returns when it has. Does nothing when none runs.
    func stop(immediately: Bool = false) async {
        let asked = handle.withLock { address -> Bool in
            guard let server = OpaquePointer(bitPattern: address) else { return false }
            hardcharger_server_request_stop(server, immediately ? 1 : 0)
            return true
        }
        guard asked else { return }
        // The watcher clears the handle once the server has stopped.
        while isRunning {
            try? await Task.sleep(for: .milliseconds(50))
        }
    }

    /// A stop request that doesn't wait: for the app going away.
    func requestStop(immediately: Bool) {
        handle.withLock { address in
            if let server = OpaquePointer(bitPattern: address) {
                hardcharger_server_request_stop(server, immediately ? 1 : 0)
            }
        }
    }

    private static func startServer(_ launch: ServerLaunch) throws -> (OpaquePointer, Int) {
        // The secrets go in through the environment only while the server reads its options.
        for (name, value) in launch.environment { setenv(name, value, 1) }
        defer { for name in launch.environment.keys { unsetenv(name) } }

        let arguments = launch.arguments.map { strdup($0) }
        defer { for pointer in arguments { free(pointer) } }
        var argv = arguments.map { UnsafePointer<CChar>($0) }
        var error = [CChar](repeating: 0, count: 1024)
        var server: OpaquePointer?
        let status = argv.withUnsafeMutableBufferPointer { buffer in
            hardcharger_server_start(Int32(buffer.count), buffer.baseAddress, &server, &error, error.count)
        }
        guard status == 0, let server else {
            throw StartFailure(status: status, message: String(cBuffer: error))
        }
        return (server, Int(hardcharger_server_port(server)))
    }

    private func watch(_ address: UInt, onExit: @escaping @Sendable (Exit) -> Void) {
        Thread.detachNewThread { [self] in
            guard let server = OpaquePointer(bitPattern: address) else { return }
            var error = [CChar](repeating: 0, count: 1024)
            let status = hardcharger_server_wait(server, &error, error.count)
            handle.withLock { $0 = 0 }
            hardcharger_server_free(server)
            let message = status == 0 ? nil : String(cBuffer: error)
            onExit(Exit(status: status, message: message))
        }
    }
}

extension String {
    /// The NUL-terminated text a C function wrote into a buffer.
    init(cBuffer buffer: [CChar]) {
        self.init(decoding: buffer.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self)
    }
}
