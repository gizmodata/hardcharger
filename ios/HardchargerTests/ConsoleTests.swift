import XCTest

@testable import Hardcharger

/// The SQL console: when input is ready to run (the desktop client's own rules), and what it
/// prints, compared with the `hardcharger` command-line client.
final class ConsoleTests: XCTestCase {
    func testReadiness() {
        func ready(_ input: String) -> Bool { ConsoleEngine.readiness(of: input).complete }
        XCTAssertFalse(ready("SELECT"))
        XCTAssertFalse(ready("SELECT\n  42\n  AS answer"), "a 3-line SELECT waits for its ';'")
        XCTAssertTrue(ready("SELECT\n  42\n  AS answer;"))
        XCTAssertFalse(ready("SELECT 'a;b'"), "';' in a string")
        XCTAssertFalse(ready("SELECT \"a;b\" FROM t"), "';' in a quoted name")
        XCTAssertFalse(ready("SELECT 1 -- ;"), "';' in a line comment")
        XCTAssertFalse(ready("SELECT /* ; */ 1"), "';' in a block comment")
        XCTAssertFalse(ready("CREATE MACRO m() AS $$ SELECT 1; $$"), "';' in a dollar quote")
        XCTAssertTrue(ready("CREATE MACRO m() AS $$ SELECT 1; $$;"))
        XCTAssertEqual(ConsoleEngine.readiness(of: "SELECT 1; SELECT 2;").statements, 2)
        XCTAssertTrue(ready("SELECT 1; SELECT 2;"))
        XCTAssertFalse(ready("SELECT 1; SELECT 2"), "the second statement is still open")
        XCTAssertTrue(ready(".tables"), "a dot command needs no ';'")
        XCTAssertTrue(ready(".mode csv"))
        XCTAssertFalse(ready("   "))
        XCTAssertFalse(ready("-- just a comment"))
    }

    /// `hardcharger --host … -c "SELECT 42 AS answer;"` at a terminal prints exactly this.
    static let cliAnswer = """
        ┌─────────┐
        │ answer  │
        │ integer │
        ├─────────┤
        │      42 │
        ├─────────┤
        │ 1 row   │
        └─────────┘

        """

    @MainActor
    func testConsoleAgainstTheEmbeddedServer() async throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "console-\(UUID().uuidString)")
        let paths = AppPaths(root: root)
        try paths.createDirectories()
        defer { try? FileManager.default.removeItem(at: root) }
        let credential = LocalCredential(username: "hardcharger", password: "console-test-only-1",
                                         secretKey: String(repeating: "cd", count: 32))
        var settings = ServerSettings()
        settings.port = 0
        let engine = EmbeddedServer()
        let port = try await engine.start(launch: ServerLaunch(settings: settings, paths: paths, credential: credential)) { _ in }
        addTeardownBlock { await engine.stop(immediately: true) }

        let console = SQLConsole(target: .local)
        await console.connect(options: {
            ConsoleEngine.Options(host: "127.0.0.1", port: port, tls: false,
                                  username: credential.username, password: credential.password)
        }, key: "test")
        XCTAssertTrue(console.isConnected, console.connectionError ?? "")
        XCTAssertEqual(console.prompt, "Hardcharger.main> ")

        // Return on an open statement adds a line; at the ';' it runs.
        console.input = "SELECT 42\nAS answer"
        XCTAssertFalse(console.returnPressed())
        XCTAssertTrue(console.entries.isEmpty)
        console.input = "SELECT 42 AS answer;"
        XCTAssertTrue(console.returnPressed())
        try await waitUntilIdle(console)
        XCTAssertEqual(console.entries.last?.output, Self.cliAnswer, "the same table as the desktop client")

        await run(console, "CREATE TABLE people AS SELECT * FROM (VALUES (1, 'Ada'), (2, 'Grace')) t(id, name);")
        XCTAssertEqual(console.entries.last?.output, "OK, 2 rows affected\n")
        await run(console, ".tables")
        XCTAssertTrue(console.entries.last?.output.contains("people") == true)

        await run(console, "SELECT 1 AS first;\nSELECT * FROM nowhere;\nSELECT 3 AS third;")
        let failed = try XCTUnwrap(console.entries.last)
        XCTAssertEqual(failed.kind, .error)
        XCTAssertTrue(failed.output.contains("SELECT * FROM nowhere;"), "the echo names the failing statement")
        XCTAssertFalse(failed.output.contains("third"), "it stops at the first error")

        await run(console, ".mode csv")
        await run(console, "SELECT name FROM people ORDER BY id;")
        XCTAssertTrue(console.entries.last?.output.hasPrefix("name\nAda\nGrace\n") == true)

        // Run (⌘↩) runs a last statement without its ';'.
        console.input = "SELECT 7 AS seven"
        console.submit()
        try await waitUntilIdle(console)
        XCTAssertTrue(console.entries.last?.output.hasPrefix("seven\n7\n") == true)

        // History: back through what ran, forward to the draft.
        console.input = "draft"
        XCTAssertTrue(console.historyBack())
        XCTAssertEqual(console.input, "SELECT 7 AS seven")
        XCTAssertTrue(console.historyForward())
        XCTAssertEqual(console.input, "draft")

        console.input = ".exit"
        console.submit()
        XCTAssertTrue(console.entries.isEmpty, ".exit clears the screen")
        console.disconnect()
    }

    @MainActor
    private func run(_ console: SQLConsole, _ input: String) async {
        console.input = input
        console.submit()
        try? await waitUntilIdle(console)
    }

    @MainActor
    private func waitUntilIdle(_ console: SQLConsole) async throws {
        try await Task.sleep(for: .milliseconds(50))
        let deadline = Date.now.addingTimeInterval(30)
        while console.isRunning, Date.now < deadline { try await Task.sleep(for: .milliseconds(20)) }
        XCTAssertFalse(console.isRunning)
    }
}
