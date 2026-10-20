import AuthenticationServices
import Foundation

/// Browser sign-in to a Warp SQL server (spec §4.3) from the app: the app creates the flow,
/// shows the identity provider in an ASWebAuthenticationSession, and polls the flow with
/// the poll secret it kept, which hands out the token once. The browser never comes back
/// to the app (the server's callback page says the sign-in is done), so the sheet is closed
/// as soon as the poll has the token; a person who closes it first still gets signed in if
/// the provider had finished.
@MainActor
enum BrowserSignIn {
    struct Cancelled: LocalizedError {
        var errorDescription: String? { String(localized: "Sign-in was cancelled.") }
    }

    struct Failed: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    /// The scheme the session waits for, which no page ever opens: the poll finishes it.
    static let callbackScheme = "hardcharger-sign-in"

    static func signIn(client: WarpClient, presentation: ASWebAuthenticationPresentationContextProviding)
        async throws -> AccessToken
    {
        let flow = try await client.startBrowserSignIn()
        let browser = BrowserSession(url: flow.authorizeURL, presentation: presentation)
        browser.start()
        defer { browser.cancel() }
        while true {
            switch try await client.poll(flow) {
            case .complete(let token):
                return token
            case .failed(let message):
                throw Failed(message: message)
            case .pending:
                if browser.isClosed {
                    // Closed by the person: the provider may have finished just before.
                    try await Task.sleep(for: .seconds(1))
                    if case .complete(let token) = try await client.poll(flow) { return token }
                    throw Cancelled()
                }
                try await Task.sleep(for: min(flow.pollInterval, .seconds(2)))
            }
        }
    }
}

/// One ASWebAuthenticationSession, kept until the poll is done with it.
@MainActor
private final class BrowserSession {
    private var session: ASWebAuthenticationSession?
    private(set) var isClosed = false

    init(url: URL, presentation: ASWebAuthenticationPresentationContextProviding) {
        let session = ASWebAuthenticationSession(url: url, callbackURLScheme: BrowserSignIn.callbackScheme) {
            [weak self] _, _ in
            Task { @MainActor in self?.isClosed = true }
        }
        session.presentationContextProvider = presentation
        // The provider's own cookies help: a person signed in there needn't type again.
        session.prefersEphemeralWebBrowserSession = false
        self.session = session
    }

    func start() {
        if session?.start() != true { isClosed = true }
    }

    func cancel() {
        session?.cancel()
        session = nil
    }
}

/// Presents sign-in sheets over the app's frontmost window.
@MainActor
final class WindowPresentation: NSObject, ASWebAuthenticationPresentationContextProviding {
    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
        let active = scenes.first { $0.activationState == .foregroundActive } ?? scenes.first
        return active?.keyWindow ?? active?.windows.first ?? ASPresentationAnchor()
    }
}
