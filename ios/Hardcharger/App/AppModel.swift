import Foundation
import Observation
import SwiftUI

/// What every window shares: the on-device server, the saved remote servers, and the
/// launch options tests and screenshots use.
@MainActor
@Observable
final class AppModel {
    let server: ServerController
    let remotes: RemoteServerStore
    let launch: LaunchOptions
    /// Files handed to the app (Files' "Open in", the share sheet) waiting for a window
    /// to import them.
    var pendingImports: [URL] = []
    /// A connection a link (hardcharger://connect?…) asks to add.
    var pendingConnection: RemoteServer?

    init(launch: LaunchOptions = .current) {
        self.launch = launch
        if launch.resetState { AppModel.resetState() }
        server = ServerController()
        remotes = RemoteServerStore()
        AppLifecycle.server = server
        if let port = launch.port { server.settings.port = port }
    }

    /// Starts the server when the app opens, if the person wants that.
    func appLaunched() async {
        // Unit tests run inside the app and start servers of their own (one per process).
        if launch.isUnitTesting { return }
        if server.settings.startAutomatically || launch.port != nil {
            await server.start()
        }
    }

    func handle(url: URL) {
        if url.isFileURL {
            pendingImports.append(url)
        } else if url.scheme == "hardcharger", let connection = Self.connection(from: url) {
            pendingConnection = connection
        }
    }

    /// hardcharger://connect?url=https://192.168.1.20:31337&fingerprint=AB:CD:…&name=…
    /// (the QR code another device's Share screen shows).
    static func connection(from url: URL) -> RemoteServer? {
        guard url.host() == "connect",
              let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems,
              let text = items.first(where: { $0.name == "url" })?.value,
              let origin = RemoteServer.origin(from: text)
        else { return nil }
        let fingerprint = items.first { $0.name == "fingerprint" }?.value
        let name = items.first { $0.name == "name" }?.value ?? origin.host() ?? "Hardcharger"
        return RemoteServer(name: name, endpoint: ChargewireEndpoint(origin: origin, pinnedFingerprint: fingerprint),
                            signIn: .password, username: LocalCredential.defaultUsername)
    }

    /// UI tests start from nothing: no settings, database, imports, logs, web view data (the
    /// web UI's saved tabs) or connections.
    private static func resetState() {
        UserDefaults.standard.removePersistentDomain(forName: Bundle.main.bundleIdentifier ?? "")
        let paths = AppPaths.standard
        for url in [paths.database, paths.database.appendingPathExtension("wal"), paths.imports, paths.exports,
                    paths.logFile, paths.previousLogFile, paths.library.appending(path: "WebKit")] {
            try? FileManager.default.removeItem(at: url)
        }
        for item in (try? Keychain.all(service: RemoteServerStore.service)) ?? [] {
            try? Keychain.delete(service: RemoteServerStore.service, account: item.account)
        }
    }
}

/// Launch arguments for UI tests and screenshots (`-HCResetState YES`, …). None of them
/// changes what a person's app does.
struct LaunchOptions: Sendable {
    var resetState = false
    /// The server's port for this run (0: any free port).
    var port: Int?
    var initialTab: AppTab?
    /// How long the splash stays at least, in seconds.
    var splashMinimum: Double = 1.2
    /// Keep showing the splash (screenshots).
    var splashOnly = false
    var initialSheet: String?
    /// XCTest hosts unit tests in the app.
    var isUnitTesting = ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil

    static var current: LaunchOptions {
        let defaults = UserDefaults.standard
        var options = LaunchOptions()
        options.resetState = defaults.bool(forKey: "HCResetState")
        if defaults.object(forKey: "HCPort") != nil { options.port = defaults.integer(forKey: "HCPort") }
        options.initialTab = defaults.string(forKey: "HCInitialTab").flatMap(AppTab.init(rawValue:))
        if defaults.object(forKey: "HCSplashMinimum") != nil {
            options.splashMinimum = defaults.double(forKey: "HCSplashMinimum")
        }
        options.splashOnly = defaults.bool(forKey: "HCSplashOnly")
        options.initialSheet = defaults.string(forKey: "HCInitialSheet")
        return options
    }
}

enum AppTab: String, Hashable, CaseIterable, Sendable {
    case web, console, server, logs, servers, settings
}

/// One window's state: its tab, which server its console shows, and its consoles.
@MainActor
@Observable
final class SceneModel {
    var tab: AppTab = .web
    /// Which server the web UI and the console show.
    var webTarget: ConsoleTarget = .local
    var sqlTarget: ConsoleTarget = .local
    var showImporter = false
    var showShare = false
    var importResults: [DataImport.Result] = []
    var importError: String?
    var isImporting = false
    /// Bumped by Reload: a remote console signs in again.
    var remoteReloads = 0

    @ObservationIgnored private var consoles: [ConsoleTarget: WebConsole] = [:]
    @ObservationIgnored private var sqlConsoles: [ConsoleTarget: SQLConsole] = [:]

    func webConsole(for target: ConsoleTarget) -> WebConsole {
        if let console = consoles[target] { return console }
        let console = WebConsole(target: target)
        consoles[target] = console
        return console
    }

    func sqlConsole(for target: ConsoleTarget) -> SQLConsole {
        if let console = sqlConsoles[target] { return console }
        let console = SQLConsole(target: target)
        sqlConsoles[target] = console
        return console
    }

    func openWeb(_ target: ConsoleTarget) {
        webTarget = target
        tab = .web
    }

    func openConsole(_ target: ConsoleTarget) {
        sqlTarget = target
        tab = .console
    }

    func importFiles(_ urls: [URL], server: ServerController) async {
        guard !urls.isEmpty else { return }
        isImporting = true
        defer { isImporting = false }
        var results: [DataImport.Result] = []
        for url in urls {
            do {
                results.append(try await DataImport.run(url, server: server))
            } catch {
                importError = error.localizedDescription
            }
        }
        if !results.isEmpty {
            importResults = results
            // The console's catalog shows the new tables after a reload.
            consoles[.local]?.reload()
        }
    }
}
