import Foundation
import Observation
import UIKit

/// The on-device server as the app shows and drives it: start, stop, restart, its state,
/// settings, credential and certificate, and access tokens for the web console.
@MainActor
@Observable
final class ServerController {
    enum Phase: Equatable, Sendable {
        case stopped
        case starting
        case running
        case stopping
        case failed(String)

        var isBusy: Bool { self == .starting || self == .stopping }
    }

    private(set) var phase: Phase = .stopped
    private(set) var port: Int?
    private(set) var startedAt: Date?
    /// The settings the running server started with.
    private(set) var runningSettings: ServerSettings?
    /// The certificate the server uses while it runs with TLS.
    private(set) var certificate: ServerCertificateFiles.Current?
    /// The device's addresses when the server last started sharing.
    private(set) var sharedAddresses: [LocalNetwork.Address] = []
    /// Set when the configured port was taken and the server runs on another for now.
    private(set) var portNote: String?

    var settings: ServerSettings {
        didSet {
            settings.save(to: defaults)
            if settings.port != oldValue.port, case .failed = phase { phase = .stopped }
            applyKeepAwake()
        }
    }

    let paths: AppPaths
    private let engine: EmbeddedServer
    private let defaults: UserDefaults
    private var token: AccessToken?
    private let bonjour = BonjourAdvertiser()
    /// Bumped on every start: a stop report from an earlier server is ignored.
    private var generation = 0

    init(paths: AppPaths = .standard, defaults: UserDefaults = .standard, engine: EmbeddedServer = EmbeddedServer()) {
        self.paths = paths
        self.defaults = defaults
        self.engine = engine
        settings = ServerSettings.load(from: defaults)
    }

    var versions: EmbeddedServer.Versions { EmbeddedServer.versions }

    /// Settings changed since the server started that need a restart.
    var needsRestart: Bool {
        guard phase == .running, let runningSettings else { return false }
        return settings.needsRestart(comparedTo: runningSettings)
    }

    var usesTLS: Bool { runningSettings?.usesTLS ?? settings.usesTLS }

    /// How the app itself reaches the server: always over loopback.
    var localEndpoint: WarpEndpoint? {
        guard phase == .running, let port else { return nil }
        let scheme = usesTLS ? "https" : "http"
        return WarpEndpoint(origin: URL(string: "\(scheme)://127.0.0.1:\(port)")!,
                            pinnedFingerprint: usesTLS ? certificate?.fingerprint : nil)
    }

    /// Where other devices open the web console while the server is shared.
    var sharedConsoleURLs: [URL] {
        guard phase == .running, runningSettings?.shareOnLocalNetwork == true, let port else { return [] }
        return sharedAddresses.compactMap { URL(string: "https://\($0.ip):\(port)/ui/") }
    }

    /// The credential, made on first use (Keychain).
    func credential() throws -> LocalCredential { try LocalCredential.loadOrCreate() }

    // MARK: - Start and stop

    func start() async {
        guard phase == .stopped || phase.isFailed else { return }
        phase = .starting
        generation += 1
        let generation = generation
        let settings = settings
        do {
            try paths.createDirectories()
            LogRotation.rotateIfNeeded(paths)
            let credential = try credential()
            if settings.usesTLS {
                sharedAddresses = settings.shareOnLocalNetwork ? LocalNetwork.addresses() : []
                let paths = paths
                let ips = sharedAddresses.map(\.ip)
                certificate = try await Task.detached {
                    try ServerCertificateFiles.ensure(paths: paths, addresses: ips, hostName: "")
                }.value
            } else {
                certificate = nil
                sharedAddresses = []
            }
            let onExit: @Sendable (EmbeddedServer.Exit) -> Void = { [weak self] exit in
                Task { @MainActor in self?.serverExited(exit, generation: generation) }
            }
            let port: Int
            portNote = nil
            do {
                port = try await engine.start(
                    launch: ServerLaunch(settings: settings, paths: paths, credential: credential), onExit: onExit)
            } catch let failure as EmbeddedServer.StartFailure
                where failure.message.localizedCaseInsensitiveContains("in use") && settings.port != 0
            {
                // The port is taken (in the simulator, by a server on the Mac): another one for
                // this run; the setting stays.
                var elsewhere = settings
                elsewhere.port = 0
                port = try await engine.start(
                    launch: ServerLaunch(settings: elsewhere, paths: paths, credential: credential), onExit: onExit)
                portNote = String(localized: "Port \(String(settings.port)) is in use on this device, so Hardcharger is on \(String(port)) for now.")
            }
            self.port = port
            runningSettings = settings
            startedAt = .now
            token = nil
            phase = .running
            applyKeepAwake()
        } catch {
            phase = .failed(error.localizedDescription)
            port = nil
        }
    }

    func stop(immediately: Bool = false) async {
        guard phase == .running else { return }
        phase = .stopping
        await engine.stop(immediately: immediately)
        if phase == .stopping { markStopped() }
    }

    func restart() async {
        await stop()
        await start()
    }

    /// The app is going away: stop at once, and give the server up to two seconds to close
    /// the database.
    func stopForTermination() {
        engine.requestStop(immediately: true)
        let deadline = Date.now.addingTimeInterval(2)
        while engine.isRunning, Date.now < deadline {
            Thread.sleep(forTimeInterval: 0.02)
        }
    }

    /// Whether the server still answers; after iOS suspended the app for long, its socket
    /// may be gone.
    func checkHealth() async -> Bool {
        guard let endpoint = localEndpoint else { return false }
        return await WarpClient(endpoint: endpoint, timeout: 3).isServing()
    }

    /// Back in the foreground: restart a server that stopped answering while suspended.
    func resumeAfterBackground() async {
        guard phase == .running else { return }
        if await !checkHealth() {
            await restart()
        }
    }

    private func serverExited(_ exit: EmbeddedServer.Exit, generation: Int) {
        guard generation == self.generation else { return }
        switch phase {
        case .stopping, .stopped:
            markStopped()
        default:
            // It stopped by itself.
            markStopped()
            if exit.status != 0 {
                phase = .failed(exit.message ?? String(localized: "The server stopped unexpectedly."))
            }
        }
    }

    private func markStopped() {
        phase = .stopped
        port = nil
        startedAt = nil
        runningSettings = nil
        token = nil
        applyKeepAwake()
    }

    // MARK: - Tokens

    /// An access token for this device's server (for the web console and imports).
    func accessToken() async throws -> AccessToken {
        if let token, token.isFresh() { return token }
        guard let endpoint = localEndpoint else {
            throw WarpError(status: 0, code: "client_not_running", detail: String(localized: "The server isn't running."))
        }
        let credential = try credential()
        let fresh = try await WarpClient(endpoint: endpoint).signIn(
            username: credential.username, password: credential.password)
        token = fresh
        return fresh
    }

    // MARK: - Credential and certificate

    /// A new administrator password; the server restarts with it.
    func regeneratePassword() async throws {
        let fresh = try credential().withNewPassword()
        try fresh.store()
        if phase == .running { await restart() }
    }

    /// A new self-signed certificate (a new fingerprint); the server restarts with it.
    func regenerateCertificate() async throws {
        let paths = paths
        let ips = LocalNetwork.addresses().map(\.ip)
        _ = try await Task.detached {
            try ServerCertificateFiles.regenerate(paths: paths, addresses: ips, hostName: "")
        }.value
        if phase == .running, usesTLS { await restart() } else { certificate = ServerCertificateFiles.load(paths: paths) }
    }

    /// Finds a free port (the configured one is taken) and saves it.
    func pickFreePort() {
        settings.port = Self.freePort() ?? Int.random(in: 40000...60000)
    }

    static func freePort() -> Int? {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var address = sockaddr_in()
        address.sin_family = sa_family_t(AF_INET)
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        address.sin_port = 0
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let bound = withUnsafeMutablePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, length) == 0 && getsockname(fd, $0, &length) == 0
            }
        }
        return bound ? Int(UInt16(bigEndian: address.sin_port)) : nil
    }

    // MARK: - Screen

    /// While sharing (and the person wants it), the screen stays on: iOS suspends an app in
    /// the background, and a suspended server answers nobody.
    private func applyKeepAwake() {
        let sharing = phase == .running && runningSettings?.shareOnLocalNetwork == true
        UIApplication.shared.isIdleTimerDisabled = sharing && settings.keepAwakeWhileSharing
        if sharing, let port {
            bonjour.advertise(port: port, name: "Hardcharger on \(UIDevice.current.name)")
        } else {
            bonjour.stop()
        }
    }

    /// A link that opens another device's Hardcharger app with this server, trusting its
    /// certificate (the QR code on the Share screen).
    func connectLink(for url: URL) -> URL? {
        guard let certificate else { return nil }
        var components = URLComponents()
        components.scheme = "hardcharger"
        components.host = "connect"
        let origin = url.deletingLastPathComponent().absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        components.queryItems = [
            URLQueryItem(name: "url", value: origin),
            URLQueryItem(name: "fingerprint", value: certificate.fingerprint),
            URLQueryItem(name: "name", value: UIDevice.current.name),
        ]
        return components.url
    }

    /// The database file's size, with its write-ahead log.
    func databaseSize() -> Int64 {
        let fm = FileManager.default
        return [paths.database, paths.database.appendingPathExtension("wal")].reduce(0) { total, url in
            let attributes = try? fm.attributesOfItem(atPath: url.path(percentEncoded: false))
            return total + ((attributes?[.size] as? NSNumber)?.int64Value ?? 0)
        }
    }
}

extension ServerController.Phase {
    var isFailed: Bool {
        if case .failed = self { return true }
        return false
    }

    var failureMessage: String? {
        if case .failed(let message) = self { return message }
        return nil
    }
}
