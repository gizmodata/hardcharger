import XCTest

@testable import Hardcharger

/// The server's command line, settings, the Keychain credential, certificates, and the small
/// parsers the app relies on.
final class ConfigurationTests: XCTestCase {
    private let paths = AppPaths(root: URL(filePath: "/container", directoryHint: .isDirectory))
    private let credential = LocalCredential(username: "hardcharger", password: "s3cret-pass", secretKey: "k")

    func testLocalLaunchListensOnLoopbackWithoutTLS() {
        let launch = ServerLaunch(settings: ServerSettings(), paths: paths, credential: credential,
                                  physicalMemory: 8 << 30)
        let args = launch.arguments
        XCTAssertEqual(args.first, "hardcharger-server")
        XCTAssertTrue(args.contains("--host=127.0.0.1"))
        XCTAssertTrue(args.contains("--port=31337"))
        XCTAssertTrue(args.contains("--database=/container/Documents/Hardcharger.duckdb"))
        XCTAssertTrue(args.contains("--web-ui=true"))
        XCTAssertTrue(args.contains("--memory-limit=2457MB"), "30% of 8 GiB")
        XCTAssertFalse(args.contains { $0.hasPrefix("--tls-") })
        XCTAssertTrue(args.contains { $0.hasPrefix("--sensitive-paths=") && $0.contains("/container/Library/WebKit") })
    }

    func testSecretsGoInTheEnvironmentNeverTheArguments() {
        let launch = ServerLaunch(settings: ServerSettings(), paths: paths, credential: credential)
        XCTAssertFalse(launch.arguments.joined().contains("s3cret-pass"))
        XCTAssertFalse(launch.redactedDescription.contains("s3cret-pass"))
        XCTAssertEqual(launch.environment["HARDCHARGER_ADMIN_PASSWORD"], "s3cret-pass")
        XCTAssertEqual(launch.environment["HARDCHARGER_SECRET_KEY"], "k")
    }

    func testSharingListensEverywhereWithTLS() {
        var settings = ServerSettings()
        settings.shareOnLocalNetwork = true
        settings.threads = 4
        let args = ServerLaunch(settings: settings, paths: paths, credential: credential).arguments
        XCTAssertTrue(args.contains("--host=0.0.0.0"))
        XCTAssertTrue(args.contains("--tls-cert=/container/Library/Application Support/Hardcharger/tls-certificate.pem"))
        XCTAssertTrue(args.contains("--tls-key=/container/Library/Application Support/Hardcharger/tls-key.pem"))
        XCTAssertTrue(args.contains { $0.hasPrefix("--init-sql-commands=") && $0.contains("SET GLOBAL threads = 4;") })
    }

    func testInitSQLQuotesPaths() {
        let odd = AppPaths(root: URL(filePath: "/it's here", directoryHint: .isDirectory))
        XCTAssertTrue(ServerLaunch.initSQL(settings: ServerSettings(), paths: odd)
            .contains("'/it''s here/Library/Caches/duckdb-spill/'"))
    }

    func testRestartIsNeededOnlyForServerSettings() {
        let running = ServerSettings()
        var changed = running
        changed.logStyle = .plain
        changed.startAutomatically = false
        XCTAssertFalse(changed.needsRestart(comparedTo: running))
        changed.printQueries = false
        XCTAssertTrue(changed.needsRestart(comparedTo: running))
    }

    func testSettingsPersist() throws {
        let defaults = try XCTUnwrap(UserDefaults(suiteName: "settings-test-\(UUID().uuidString)"))
        var settings = ServerSettings.load(from: defaults)
        XCTAssertEqual(settings, ServerSettings())
        settings.port = 40000
        settings.memoryLimit = .gb2
        settings.save(to: defaults)
        XCTAssertEqual(ServerSettings.load(from: defaults), settings)
    }

    func testGeneratedPasswordsAreLongAndSafeForEveryClient() throws {
        var seen = Set<String>()
        for _ in 0..<50 {
            let password = try LocalCredential.randomPassword()
            XCTAssertEqual(password.count, LocalCredential.passwordLength)
            XCTAssertTrue(password.allSatisfy { $0.isLetter || $0.isNumber }, password)
            for separator in [":", "@", "/", ";", " ", "'", "\""] { XCTAssertFalse(password.contains(separator)) }
            seen.insert(password)
        }
        XCTAssertEqual(seen.count, 50)
    }

    func testCredentialIsMadeOnceAndKeptInTheKeychain() throws {
        try Keychain.delete(service: LocalCredential.service, account: LocalCredential.account)
        let first = try LocalCredential.loadOrCreate()
        let again = try LocalCredential.loadOrCreate()
        XCTAssertEqual(first, again)
        XCTAssertEqual(first.username, "hardcharger")
        XCTAssertEqual(first.secretKey.count, 64)
        let renewed = try first.withNewPassword()
        try renewed.store()
        XCTAssertEqual(try LocalCredential.loadOrCreate().password, renewed.password)
        XCTAssertNotEqual(renewed.password, first.password)
        XCTAssertEqual(renewed.secretKey, first.secretKey)
    }

    func testSelfSignedCertificateParsesAndNamesTheAddresses() throws {
        let certificate = try SelfSignedCertificate.make(commonName: "Hardcharger", dnsNames: ["localhost"],
                                                         ipAddresses: ["127.0.0.1", "192.168.1.20"])
        XCTAssertNotNil(SecCertificateCreateWithData(nil, certificate.der as CFData))
        XCTAssertEqual(certificate.fingerprint.count, 32 * 3 - 1)
        XCTAssertTrue(certificate.certificatePEM.hasPrefix("-----BEGIN CERTIFICATE-----"))
        XCTAssertTrue(certificate.privateKeyPEM.contains("PRIVATE KEY"))
        XCTAssertLessThanOrEqual(certificate.notAfter.timeIntervalSinceNow, 398 * 24 * 3600)
        XCTAssertEqual(SelfSignedCertificate.der(fromPEM: certificate.certificatePEM), certificate.der)
        // The address's SAN ([7] 4 bytes) is in it.
        XCTAssertNotNil(certificate.der.range(of: Data([0x87, 4, 192, 168, 1, 20])))
    }

    func testCertificateIsRemadeForANewAddress() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "cert-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        let paths = AppPaths(root: root)
        let first = try ServerCertificateFiles.ensure(paths: paths, addresses: ["10.0.0.5"], hostName: "")
        XCTAssertEqual(try ServerCertificateFiles.ensure(paths: paths, addresses: ["10.0.0.5"], hostName: "").fingerprint,
                       first.fingerprint, "kept while the address is the same")
        let moved = try ServerCertificateFiles.ensure(paths: paths, addresses: ["10.0.0.6"], hostName: "")
        XCTAssertNotEqual(moved.fingerprint, first.fingerprint)
        XCTAssertTrue(moved.ipAddresses.contains("10.0.0.6"))
    }

    func testLogLinesParseWithAndWithoutMarks() {
        let ready = LogLine.parse("2026-10-20T10:46:09.923Z info    Hardcharger is ready version=2", id: 0)
        XCTAssertEqual(ready.level, .info)
        XCTAssertEqual(ready.emoji, "✅")
        XCTAssertEqual(ready.text(style: .plain), "10:46:09.923 info    Hardcharger is ready version=2")
        let marked = LogLine.parse("2026-10-20T10:46:09.923Z ⚠️ warning no secret_key", id: 1)
        XCTAssertEqual(marked.level, .warning)
        XCTAssertEqual(marked.message, "no secret_key")
        XCTAssertEqual(marked.text(style: .plain), "10:46:09.923 warning no secret_key")
        XCTAssertTrue(marked.text(style: .emoji).contains("⚠️"))
        let other = LogLine.parse("usage: hardcharger-server", id: 2)
        XCTAssertNil(other.level)
        XCTAssertEqual(other.text(style: .emoji), "usage: hardcharger-server")
    }

    func testImportTableNames() {
        XCTAssertEqual(DataImport.tableName(for: URL(filePath: "/x/Sales 2026.csv")), "sales_2026")
        XCTAssertEqual(DataImport.tableName(for: URL(filePath: "/x/2026-q1.parquet")), "t_2026_q1")
        XCTAssertEqual(DataImport.tableName(for: URL(filePath: "/x/über.json")), "uber")
        XCTAssertEqual(DataImport.format(of: URL(filePath: "/x/a.TSV")), .csv)
        XCTAssertNil(DataImport.format(of: URL(filePath: "/x/a.xlsx")))
        XCTAssertEqual(DataImport.statement(table: "a\"b", file: URL(filePath: "/d/it's.csv"), format: .csv),
                       "CREATE OR REPLACE TABLE \"a\"\"b\" AS SELECT * FROM read_csv('/d/it''s.csv')")
    }

    func testRemoteAddresses() {
        XCTAssertEqual(RemoteServer.origin(from: "db.example.com")?.absoluteString, "https://db.example.com:31337")
        XCTAssertEqual(RemoteServer.origin(from: "192.168.1.20:4000")?.absoluteString, "http://192.168.1.20:4000")
        XCTAssertEqual(RemoteServer.origin(from: "https://db.example.com:443/ui/")?.absoluteString,
                       "https://db.example.com:443")
        XCTAssertNil(RemoteServer.origin(from: "ftp://x"))
        XCTAssertNil(RemoteServer.origin(from: "  "))
    }

    @MainActor
    func testConnectLinks() throws {
        let link = URL(string: "hardcharger://connect?url=https://192.168.1.20:31337&fingerprint=AB:CD&name=Phone")!
        let server = try XCTUnwrap(AppModel.connection(from: link))
        XCTAssertEqual(server.name, "Phone")
        XCTAssertEqual(server.endpoint.origin.absoluteString, "https://192.168.1.20:31337")
        XCTAssertEqual(server.endpoint.pinnedFingerprint, "AB:CD")
    }

    @MainActor
    func testWebSignInScriptIsForItsOriginOnly() {
        XCTAssertEqual(WebConsole.browserOrigin(URL(string: "https://db.example.com:443")!), "https://db.example.com")
        XCTAssertEqual(WebConsole.browserOrigin(URL(string: "http://127.0.0.1:31337")!), "http://127.0.0.1:31337")
        let token = AccessToken(token: "tok\"en", expiresAt: .now.addingTimeInterval(60), username: "u")
        let source = WebConsole.signInScript(origin: URL(string: "http://127.0.0.1:31337")!, token: token).source
        XCTAssertTrue(source.contains("location.origin !== \"http://127.0.0.1:31337\""))
        XCTAssertTrue(source.contains("hardcharger-ui:sign-in"))
        XCTAssertTrue(source.contains("tok\\\"en"), "the token is a JSON string literal")
        XCTAssertFalse(source.contains("localStorage"))
    }
}
