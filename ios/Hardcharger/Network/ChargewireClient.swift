import CryptoKit
import Foundation
import Synchronization

/// An access token from a Chargewire server (spec §4).
struct AccessToken: Sendable, Equatable, Codable {
    let token: String
    let expiresAt: Date
    /// Who signed in, when the app knows (empty after browser sign-in).
    let username: String

    /// Good for a while yet (a page load or a statement won't outlive it).
    func isFresh(at now: Date = .now) -> Bool { expiresAt.timeIntervalSince(now) > 300 }

    /// A short, stable name for this token, never the token itself: the web console uses it
    /// to sign in once per token.
    var identifier: String {
        SHA256.hash(data: Data(token.utf8)).prefix(8).map { String(format: "%02x", $0) }.joined()
    }
}

/// A Chargewire problem document (spec §11), or a transport failure.
struct ChargewireError: LocalizedError, Sendable, Equatable {
    let status: Int
    let code: String
    let detail: String
    var errorDescription: String? { detail }

    static func transport(_ error: Error) -> ChargewireError {
        ChargewireError(status: 0, code: "client_transport_error", detail: error.localizedDescription)
    }
}

/// What `GET /chargewire` says about a server (spec §3.1).
struct Discovery: Sendable, Equatable {
    let serverName: String
    let serverVersion: String
    let capabilities: [String]
    let grantTypes: [String]
    /// The identity provider's name for browser sign-in (§4.3), when offered.
    let browserSignInProvider: String?
    var offersBrowserSignIn: Bool { capabilities.contains("oauth_browser") }
    var offersPassword: Bool { grantTypes.contains("password") }
}

/// A browser sign-in in progress (spec §4.3.1). The poll secret stays in memory.
struct BrowserSignInFlow: Sendable, Equatable {
    let flowID: String
    let pollSecret: String
    let authorizeURL: URL
    let pollInterval: Duration
}

enum BrowserSignInState: Sendable, Equatable {
    case pending
    case complete(AccessToken)
    case failed(String)
}

/// Where a Chargewire server is, and which certificate to trust when it is self-signed.
struct ChargewireEndpoint: Sendable, Hashable, Codable {
    /// The server's origin: scheme, host and port (`https://db.example.com:31337`).
    var origin: URL
    /// The SHA-256 fingerprint (colon-separated hex) of the one certificate to accept even
    /// though no authority signed it; nil: the system's trust decides.
    var pinnedFingerprint: String?

    var chargewireBase: URL { origin.appending(path: "chargewire") }
    var consoleURL: URL { origin.appending(path: "ui/", directoryHint: .isDirectory) }
}

/// The client side of the protocol the app needs: discovery, health, sign-in (password and
/// browser), and statements that return no rows (imports). Results are Arrow streams,
/// which the web console reads; the app itself never needs rows.
final class ChargewireClient: Sendable {
    let endpoint: ChargewireEndpoint
    private let session: URLSession
    private let trust: TrustEvaluator

    init(endpoint: ChargewireEndpoint, timeout: TimeInterval = 30) {
        self.endpoint = endpoint
        trust = TrustEvaluator(pinnedFingerprint: endpoint.pinnedFingerprint)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = timeout
        configuration.httpAdditionalHeaders = ["User-Agent": "Hardcharger-iOS/\(EmbeddedServer.versions.hardcharger)"]
        session = URLSession(configuration: configuration, delegate: trust, delegateQueue: nil)
    }

    deinit { session.finishTasksAndInvalidate() }

    /// The fingerprint of the certificate the server presented last, for "trust this
    /// certificate" after a self-signed server failed the system's check.
    var presentedFingerprint: String? { trust.lastPresented.map { SelfSignedCertificate.fingerprint(of: $0) } }
    /// The certificate itself (DER).
    var presentedCertificate: Data? { trust.lastPresented }

    func discovery() async throws -> Discovery {
        let json = try await object(for: request(path: ""))
        let server = json["server"] as? [String: Any] ?? [:]
        let auth = json["auth"] as? [String: Any] ?? [:]
        let browser = auth["oauth_browser"] as? [String: Any]
        return Discovery(
            serverName: server["name"] as? String ?? "",
            serverVersion: server["version"] as? String ?? "",
            capabilities: json["capabilities"] as? [String] ?? [],
            grantTypes: auth["grant_types"] as? [String] ?? [],
            browserSignInProvider: (browser?["provider"] as? String).flatMap { $0.isEmpty ? nil : $0 })
    }

    /// Whether the server says it serves (`200` from `/chargewire/v1/health`).
    func isServing() async -> Bool {
        guard let (_, response) = try? await session.data(for: request(path: "v1/health")) else { return false }
        return (response as? HTTPURLResponse)?.statusCode == 200
    }

    func signIn(username: String, password: String) async throws -> AccessToken {
        var req = request(path: "v1/auth/token", method: "POST")
        req.httpBody = try JSONSerialization.data(withJSONObject: [
            "grant_type": "password", "username": username, "password": password,
        ])
        return try token(from: try await object(for: req), username: username)
    }

    func startBrowserSignIn() async throws -> BrowserSignInFlow {
        var req = request(path: "v1/auth/oauth/flows", method: "POST")
        req.httpBody = Data("{}".utf8)
        let json = try await object(for: req, expecting: [201, 200])
        guard let id = json["flow_id"] as? String, let secret = json["poll_secret"] as? String,
              let urlText = json["authorize_url"] as? String, let url = URL(string: urlText)
        else { throw ChargewireError(status: 0, code: "client_protocol_error", detail: "The server started no sign-in.") }
        let interval = (json["poll_interval_s"] as? Double) ?? 2
        return BrowserSignInFlow(flowID: id, pollSecret: secret, authorizeURL: url,
                                 pollInterval: .milliseconds(Int(max(interval, 1) * 1000)))
    }

    func poll(_ flow: BrowserSignInFlow) async throws -> BrowserSignInState {
        var req = request(path: "v1/auth/oauth/flows/\(flow.flowID)", method: "POST")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["poll_secret": flow.pollSecret])
        let json = try await object(for: req)
        switch json["state"] as? String {
        case "complete":
            return .complete(try token(from: json, username: ""))
        case "failed":
            let error = json["error"] as? [String: Any]
            return .failed(error?["detail"] as? String ?? "The identity provider did not sign you in.")
        default:
            return .pending
        }
    }

    /// Runs one statement that returns no rows in a session of its own; returns the rows it
    /// changed, when the engine counts them (spec §6.2 `rows_affected`).
    func execute(_ sql: String, token: AccessToken) async throws -> Int? {
        var open = request(path: "v1/sessions", method: "POST", token: token)
        open.httpBody = Data("{}".utf8)
        guard let sessionID = try await object(for: open, expecting: [200, 201])["session_id"] as? String else {
            throw ChargewireError(status: 0, code: "client_protocol_error", detail: "The server opened no session.")
        }
        defer {
            let close = request(path: "v1/sessions/\(sessionID)", method: "DELETE", token: token)
            Task { [session] in _ = try? await session.data(for: close) }
        }
        var submit = request(path: "v1/sessions/\(sessionID)/statements", method: "POST", token: token)
        submit.setValue("wait=300", forHTTPHeaderField: "Prefer")
        submit.timeoutInterval = 330
        submit.httpBody = try JSONSerialization.data(withJSONObject: ["sql": sql])
        var document = try await object(for: submit, expecting: [200, 201])
        // Still running after the wait: follow it until it ends.
        while let state = document["state"] as? String, state == "queued" || state == "running",
              let id = document["statement_id"] as? String {
            var status = request(path: "v1/sessions/\(sessionID)/statements/\(id)", token: token)
            status.setValue("wait=30", forHTTPHeaderField: "Prefer")
            document = try await object(for: status)
        }
        if let state = document["state"] as? String, state != "succeeded" {
            let error = document["error"] as? [String: Any]
            throw ChargewireError(status: 0, code: error?["code"] as? String ?? "execution_error",
                            detail: error?["detail"] as? String ?? "The statement \(state).")
        }
        return (document["result"] as? [String: Any])?["rows_affected"] as? Int
    }

    // MARK: - HTTP

    private func request(path: String, method: String = "GET", token: AccessToken? = nil) -> URLRequest {
        var req = URLRequest(url: path.isEmpty ? endpoint.chargewireBase : endpoint.chargewireBase.appending(path: path))
        req.httpMethod = method
        req.cachePolicy = .reloadIgnoringLocalCacheData
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if method != "GET" { req.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        if let token { req.setValue("Bearer \(token.token)", forHTTPHeaderField: "Authorization") }
        return req
    }

    private func object(for request: URLRequest, expecting: Set<Int> = [200]) async throws -> [String: Any] {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw ChargewireError.transport(error)
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        guard expecting.contains(status) else {
            throw ChargewireError(
                status: status, code: json["code"] as? String ?? "client_http_\(status)",
                detail: json["detail"] as? String ?? json["title"] as? String
                    ?? HTTPURLResponse.localizedString(forStatusCode: status).capitalized)
        }
        return json
    }

    private func token(from json: [String: Any], username: String) throws -> AccessToken {
        guard let token = json["access_token"] as? String, !token.isEmpty else {
            throw ChargewireError(status: 0, code: "client_protocol_error", detail: "The server answered without a token.")
        }
        let lifetime = (json["expires_in"] as? Double).flatMap { $0 > 0 ? $0 : nil } ?? 3600
        return AccessToken(token: token, expiresAt: .now.addingTimeInterval(lifetime), username: username)
    }
}

/// TLS trust for the app's own connections: the system's decision, except for the one
/// self-signed certificate an endpoint pins by fingerprint (this device's server when it
/// shares, or a server the person chose to trust).
final class TrustEvaluator: NSObject, URLSessionDelegate, Sendable {
    let pinnedFingerprint: String?
    private let presented = Mutex<Data?>(nil)

    init(pinnedFingerprint: String?) {
        self.pinnedFingerprint = pinnedFingerprint
    }

    /// The leaf certificate (DER) the server presented last.
    var lastPresented: Data? { presented.withLock { $0 } }

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge) async
        -> (URLSession.AuthChallengeDisposition, URLCredential?)
    {
        evaluate(challenge)
    }

    /// The decision for a server-trust challenge (the web console asks the same).
    func evaluate(_ challenge: URLAuthenticationChallenge) -> (URLSession.AuthChallengeDisposition, URLCredential?) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust
        else { return (.performDefaultHandling, nil) }
        let leaf = (SecTrustCopyCertificateChain(trust) as? [SecCertificate])?.first
        let der = leaf.map { SecCertificateCopyData($0) as Data }
        let fingerprint = der.map { SelfSignedCertificate.fingerprint(of: $0) }
        presented.withLock { $0 = der }
        if let pinnedFingerprint, let fingerprint, fingerprint == pinnedFingerprint {
            return (.useCredential, URLCredential(trust: trust))
        }
        return (.performDefaultHandling, nil)
    }
}
