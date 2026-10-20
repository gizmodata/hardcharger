import XCTest

@testable import Hardcharger

/// The embedded server through the app's own wrapper: start, sign in, run, stop, start
/// again, refusals, and TLS with the device's self-signed certificate.
final class ServerBridgeTests: XCTestCase {
    private var root: URL!
    private var paths: AppPaths!
    private let engine = EmbeddedServer()
    private let credential = LocalCredential(username: "hardcharger", password: "bridge-test-only-1",
                                             secretKey: String(repeating: "ab", count: 32))

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appending(path: "bridge-\(UUID().uuidString)")
        paths = AppPaths(root: root)
        try paths.createDirectories()
    }

    override func tearDown() async throws {
        await engine.stop(immediately: true)
        try? FileManager.default.removeItem(at: root)
    }

    private func settings(tls: Bool = false) -> ServerSettings {
        var settings = ServerSettings()
        settings.port = 0
        settings.alwaysUseTLS = tls
        return settings
    }

    func testStartQueryStopAndStartAgain() async throws {
        let launch = ServerLaunch(settings: settings(), paths: paths, credential: credential)
        let exits = Exits()
        for round in 1...2 {
            let port = try await engine.start(launch: launch) { exits.add($0) }
            XCTAssertGreaterThan(port, 0)
            XCTAssertTrue(engine.isRunning)
            let client = WarpClient(endpoint: WarpEndpoint(origin: URL(string: "http://127.0.0.1:\(port)")!))
            let serving = await client.isServing()
            XCTAssertTrue(serving)
            let discovery = try await client.discovery()
            XCTAssertEqual(discovery.serverName, "Hardcharger")
            XCTAssertTrue(discovery.serverVersion.hasPrefix(EmbeddedServer.versions.hardcharger))
            let token = try await client.signIn(username: credential.username, password: credential.password)
            XCTAssertTrue(token.isFresh())
            if round == 1 {
                let rows = try await client.execute("CREATE TABLE t AS SELECT * FROM range(5)", token: token)
                XCTAssertEqual(rows, 5)
            } else {
                // The table outlived the restart; the secret key kept the token format.
                let rows = try await client.execute("INSERT INTO t SELECT * FROM range(3)", token: token)
                XCTAssertEqual(rows, 3)
            }
            await engine.stop()
            XCTAssertFalse(engine.isRunning)
        }
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(exits.all.map(\.status), [0, 0], "both servers stopped cleanly")
    }

    func testWrongPasswordIsRefused() async throws {
        let port = try await engine.start(launch: ServerLaunch(settings: settings(), paths: paths, credential: credential)) { _ in }
        let client = WarpClient(endpoint: WarpEndpoint(origin: URL(string: "http://127.0.0.1:\(port)")!))
        do {
            _ = try await client.signIn(username: credential.username, password: "wrong")
            XCTFail("signed in with a wrong password")
        } catch let error as WarpError {
            XCTAssertEqual(error.status, 401)
            XCTAssertEqual(error.code, "invalid_credentials")
        }
    }

    func testSecondServerAndBadOptionsAreRefusedWithTheReason() async throws {
        _ = try await engine.start(launch: ServerLaunch(settings: settings(), paths: paths, credential: credential)) { _ in }
        let second = EmbeddedServer()
        do {
            _ = try await second.start(launch: ServerLaunch(settings: settings(), paths: paths, credential: credential)) { _ in }
            XCTFail("a second server started")
        } catch let failure as EmbeddedServer.StartFailure {
            XCTAssertEqual(failure.status, 3)
            XCTAssertTrue(failure.message.contains("already running"), failure.message)
        }
        await engine.stop()

        var bad = ServerLaunch(settings: settings(), paths: paths, credential: credential)
        bad.arguments.append("--memory-limit=lots")
        do {
            _ = try await engine.start(launch: bad) { _ in }
            XCTFail("started with a bad option")
        } catch let failure as EmbeddedServer.StartFailure {
            XCTAssertFalse(failure.message.isEmpty)
        }
    }

    func testTLSWithTheDevicesCertificateIsTrustedOnlyByItsFingerprint() async throws {
        let certificate = try ServerCertificateFiles.ensure(paths: paths, addresses: [], hostName: "")
        let port = try await engine.start(launch: ServerLaunch(settings: settings(tls: true), paths: paths,
                                                               credential: credential)) { _ in }
        let origin = URL(string: "https://127.0.0.1:\(port)")!
        let pinned = await WarpClient(endpoint: WarpEndpoint(origin: origin, pinnedFingerprint: certificate.fingerprint))
            .isServing()
        XCTAssertTrue(pinned, "the pinned certificate is accepted")
        let unpinned = await WarpClient(endpoint: WarpEndpoint(origin: origin)).isServing()
        XCTAssertFalse(unpinned, "a self-signed certificate isn't trusted without the pin")
        let other = await WarpClient(endpoint: WarpEndpoint(origin: origin, pinnedFingerprint: String(
            repeating: "AB:", count: 31) + "AB")).isServing()
        XCTAssertFalse(other, "another fingerprint isn't accepted")
    }

    func testVersionsAndNotices() {
        let versions = EmbeddedServer.versions
        XCTAssertFalse(versions.hardcharger.isEmpty)
        XCTAssertTrue(versions.duckdb.hasPrefix("v"))
        XCTAssertFalse(versions.warpSQL.isEmpty)
        XCTAssertFalse(versions.webUI.isEmpty, "the web UI is built in")
        XCTAssertTrue(EmbeddedServer.thirdPartyNotices.contains("DuckDB"))
    }
}

private final class Exits: @unchecked Sendable {
    private let lock = NSLock()
    private var exits: [EmbeddedServer.Exit] = []
    func add(_ exit: EmbeddedServer.Exit) { lock.withLock { exits.append(exit) } }
    var all: [EmbeddedServer.Exit] { lock.withLock { exits } }
}
