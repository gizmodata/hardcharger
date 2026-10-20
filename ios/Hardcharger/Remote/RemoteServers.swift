import Foundation
import Observation

/// A Hardcharger server elsewhere, saved with how to sign in to it. Saved connections are
/// kept whole in the Keychain (one item each), password included.
struct RemoteServer: Codable, Identifiable, Hashable, Sendable {
    enum SignIn: String, Codable, CaseIterable, Identifiable, Sendable {
        /// User name and password (§4.1 password grant).
        case password
        /// The server's identity provider in a browser (§4.3).
        case browser
        var id: String { rawValue }
        var label: LocalizedStringResource {
            switch self {
            case .password: "User name and password"
            case .browser: "Single sign-on (browser)"
            }
        }
    }

    var id: UUID = UUID()
    var name: String
    var endpoint: WarpEndpoint
    var signIn: SignIn = .password
    var username: String = ""
    var password: String = ""
    /// What the server said about itself when last reached.
    var serverVersion: String?
    var browserProvider: String?
    /// The certificate the person chose to trust (PEM), when the server's isn't from an
    /// authority: the console trusts exactly it (its fingerprint pins it everywhere else).
    var trustedCertificatePEM: String?

    var displayHost: String {
        var text = endpoint.origin.host() ?? endpoint.origin.absoluteString
        if let port = endpoint.origin.port { text += ":\(port)" }
        return text
    }

    /// The origin a person types: `db.example.com`, `https://db.example.com:31337`,
    /// `192.168.1.20:31337`, or a console link (`…/ui/`). Without a scheme: https, except
    /// for addresses on a local network, where http is common.
    static func origin(from text: String) -> URL? {
        var trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        if !trimmed.contains("://") {
            let host = trimmed.split(separator: "/").first.map(String.init) ?? trimmed
            let bare = host.split(separator: ":").first.map(String.init) ?? host
            trimmed = (isLocalHost(bare) ? "http://" : "https://") + trimmed
        }
        guard var components = URLComponents(string: trimmed),
              let scheme = components.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = components.host, !host.isEmpty
        else { return nil }
        components.scheme = scheme
        components.path = ""
        components.query = nil
        components.fragment = nil
        components.user = nil
        components.password = nil
        if components.port == nil { components.port = 31337 }
        return components.url
    }

    /// Hosts on a local network (where plain HTTP is allowed: NSAllowsLocalNetworking).
    static func isLocalHost(_ host: String) -> Bool {
        let h = host.lowercased()
        if h == "localhost" || h.hasSuffix(".local") || !h.contains(".") { return true }
        let parts = h.split(separator: ".").compactMap { Int($0) }
        guard parts.count == 4 else { return false }
        return parts[0] == 10 || parts[0] == 127 || (parts[0] == 192 && parts[1] == 168)
            || (parts[0] == 172 && (16...31).contains(parts[1])) || (parts[0] == 169 && parts[1] == 254)
    }
}

/// The saved connections, in the Keychain, and the tokens signed in with them (in memory).
@MainActor
@Observable
final class RemoteServerStore {
    static let service = "com.gizmodata.hardcharger.remote-servers"

    private(set) var servers: [RemoteServer] = []
    private(set) var loadError: String?
    /// Tokens per server, for this run of the app only.
    private var tokens: [UUID: AccessToken] = [:]

    init(load: Bool = true) {
        if load { reload() }
    }

    func reload() {
        do {
            servers = try Keychain.all(service: Self.service)
                .compactMap { try? JSONDecoder().decode(RemoteServer.self, from: $0.data) }
                .sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
            loadError = nil
        } catch {
            loadError = String(describing: error)
        }
    }

    func server(_ id: UUID) -> RemoteServer? { servers.first { $0.id == id } }

    func save(_ server: RemoteServer) throws {
        try Keychain.write(try JSONEncoder().encode(server), service: Self.service, account: server.id.uuidString,
                           label: "Hardcharger: \(server.name)")
        tokens[server.id] = nil
        reload()
    }

    func delete(_ server: RemoteServer) throws {
        try Keychain.delete(service: Self.service, account: server.id.uuidString)
        tokens[server.id] = nil
        reload()
    }

    func token(for id: UUID) -> AccessToken? {
        guard let token = tokens[id], token.isFresh() else { return nil }
        return token
    }

    func remember(_ token: AccessToken, for id: UUID) { tokens[id] = token }
    func forgetToken(for id: UUID) { tokens[id] = nil }
}
