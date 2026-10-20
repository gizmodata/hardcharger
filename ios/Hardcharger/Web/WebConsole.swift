import Foundation
import Observation
import UIKit
import WebKit

/// Which server a console shows.
enum ConsoleTarget: Hashable, Sendable {
    case local
    case remote(UUID)
}

/// Hardcharger's own web console (the server's /ui/) in a WKWebView, signed in by the app.
///
/// Signing in: the app gets an access token itself (password grant, or browser sign-in for
/// a remote server) and hands it to the page the way the console keeps one: in the tab's
/// sessionStorage under `hardcharger-ui:sign-in` (ui/src/lib/auth.ts), before the page's
/// scripts run, on that server's origin only. The token never goes in a URL, a cookie or
/// localStorage. It is handed over once per token, so signing out in the console holds
/// until the app signs in again (Reload).
@MainActor
@Observable
final class WebConsole {
    let target: ConsoleTarget
    let webView: WKWebView
    private(set) var isLoading = false
    private(set) var progress: Double = 0
    private(set) var errorMessage: String?
    private(set) var loadedOrigin: URL?
    /// The last file the console downloaded (in Documents/Exports).
    var lastDownload: URL?

    @ObservationIgnored private let coordinator: Coordinator
    @ObservationIgnored private var observations: [NSKeyValueObservation] = []

    init(target: ConsoleTarget) {
        self.target = target
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.defaultWebpagePreferences.preferredContentMode = .recommended
        configuration.allowsInlineMediaPlayback = true
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.allowsBackForwardNavigationGestures = false
        webView.isInspectable = true  // Safari's Web Inspector, for the app's developers
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.accessibilityIdentifier = "console.webview"
        webView.underPageBackgroundColor = .systemBackground
        self.webView = webView
        coordinator = Coordinator()
        coordinator.console = self
        webView.navigationDelegate = coordinator
        webView.uiDelegate = coordinator
        observations = [
            webView.observe(\.estimatedProgress) { [weak self] view, _ in
                let value = view.estimatedProgress
                Task { @MainActor in self?.progress = value }
            },
            webView.observe(\.isLoading) { [weak self] view, _ in
                let value = view.isLoading
                Task { @MainActor in self?.isLoading = value }
            },
        ]
    }

    /// Loads the console of `endpoint`, signed in with `token`.
    func load(endpoint: WarpEndpoint, token: AccessToken?) {
        errorMessage = nil
        coordinator.pinnedFingerprint = endpoint.pinnedFingerprint
        coordinator.origin = endpoint.origin
        let controller = webView.configuration.userContentController
        controller.removeAllUserScripts()
        controller.addUserScript(Self.editorFocusScript)
        if let token {
            controller.addUserScript(Self.signInScript(origin: endpoint.origin, token: token))
        }
        loadedOrigin = endpoint.origin
        webView.load(URLRequest(url: endpoint.consoleURL))
    }

    func reload() { webView.reload() }

    /// Leaves the console, e.g. when its server stopped.
    func unload() {
        webView.configuration.userContentController.removeAllUserScripts()
        webView.loadHTMLString("", baseURL: nil)
        loadedOrigin = nil
    }

    fileprivate func failed(_ message: String) {
        errorMessage = message
    }

    fileprivate func clearError() { errorMessage = nil }

    /// The script that signs the console in: on `origin` only, once per token.
    static func signInScript(origin: URL, token: AccessToken) -> WKUserScript {
        let signIn: [String: Any] = [
            "username": token.username,
            "token": token.token,
            "expiresAt": Int(token.expiresAt.timeIntervalSince1970 * 1000),
        ]
        let payload = String(data: try! JSONSerialization.data(withJSONObject: signIn, options: .withoutEscapingSlashes), encoding: .utf8)!
        let originLiteral = jsString(browserOrigin(origin))
        let source = """
            (() => {
              if (location.origin !== \(originLiteral)) return;
              try {
                const id = \(jsString(token.identifier));
                if (sessionStorage.getItem("hardcharger-ios:token") === id) return;
                sessionStorage.setItem("hardcharger-ui:sign-in", JSON.stringify(\(payload)));
                sessionStorage.setItem("hardcharger-ui:user-name", \(jsString(token.username)));
                sessionStorage.setItem("hardcharger-ios:token", id);
              } catch (e) {}
            })();
            """
        return WKUserScript(source: source, injectionTime: .atDocumentStart, forMainFrameOnly: true)
    }

    /// An origin as `location.origin` spells it: no default port, no trailing slash.
    static func browserOrigin(_ url: URL) -> String {
        let scheme = url.scheme?.lowercased() ?? "http"
        var host = url.host(percentEncoded: false) ?? ""
        if host.contains(":") { host = "[\(host)]" }  // IPv6
        let defaultPort = scheme == "https" ? 443 : 80
        if let port = url.port, port != defaultPort { return "\(scheme)://\(host):\(port)" }
        return "\(scheme)://\(host)"
    }

    /// The SQL editor (Monaco) takes a tap through its own gesture handling and focuses its
    /// hidden <textarea> from there; WebKit in an app opens the keyboard only for focus that
    /// happens while the touch itself is handled, so a tap on the editor gave a cursor and no
    /// keyboard (as when the page focused the editor on load). This focuses that textarea
    /// during the touch; Monaco then places the cursor as before.
    static let editorFocusScript = WKUserScript(source: """
        document.addEventListener("touchend", (event) => {
          const editor = event.target instanceof Element ? event.target.closest(".monaco-editor") : null;
          const input = editor ? editor.querySelector("textarea.inputarea") : null;
          if (!input) return;
          // Focused already (the page focuses the editor when it loads, without a keyboard):
          // focus again, now during the touch.
          if (document.activeElement === input) input.blur();
          input.focus({ preventScroll: true });
        }, { capture: true, passive: true });
        """, injectionTime: .atDocumentEnd, forMainFrameOnly: true)

    static func jsString(_ text: String) -> String {
        let data = try! JSONSerialization.data(withJSONObject: [text], options: .withoutEscapingSlashes)
        let array = String(data: data, encoding: .utf8)!
        return String(array.dropFirst().dropLast())
    }

    // MARK: - Delegate

    @MainActor
    private final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
        weak var console: WebConsole?
        var pinnedFingerprint: String?
        var origin: URL?
        private var downloads: [ObjectIdentifier: URL] = [:]

        func webView(_ webView: WKWebView, respondTo challenge: URLAuthenticationChallenge) async
            -> (URLSession.AuthChallengeDisposition, URLCredential?)
        {
            TrustEvaluator(pinnedFingerprint: pinnedFingerprint).evaluate(challenge)
        }

        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                     preferences: WKWebpagePreferences) async -> (WKNavigationActionPolicy, WKWebpagePreferences)
        {
            if navigationAction.shouldPerformDownload { return (.download, preferences) }
            guard let url = navigationAction.request.url else { return (.allow, preferences) }
            // The console's own pages, blob and data URLs stay here; other sites (docs,
            // gizmodata.com) open in Safari.
            if let scheme = url.scheme, ["http", "https"].contains(scheme), let origin,
               !Self.sameOrigin(url, origin), navigationAction.targetFrame?.isMainFrame ?? true {
                await UIApplication.shared.open(url)
                return (.cancel, preferences)
            }
            return (.allow, preferences)
        }

        func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse) async
            -> WKNavigationResponsePolicy
        {
            navigationResponse.canShowMIMEType ? .allow : .download
        }

        func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
            download.delegate = self
        }

        func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
            download.delegate = self
        }

        func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                      suggestedFilename: String) async -> URL?
        {
            let exports = AppPaths.standard.exports
            try? FileManager.default.createDirectory(at: exports, withIntermediateDirectories: true)
            let destination = Self.unique(exports.appending(path: suggestedFilename))
            downloads[ObjectIdentifier(download)] = destination
            return destination
        }

        func downloadDidFinish(_ download: WKDownload) {
            console?.lastDownload = downloads.removeValue(forKey: ObjectIdentifier(download))
        }

        func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
            downloads[ObjectIdentifier(download)] = nil
            console?.failed(error.localizedDescription)
        }

        func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
            console?.clearError()
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                     withError error: Error)
        {
            let error = error as NSError
            if error.code == NSURLErrorCancelled { return }
            console?.failed(error.localizedDescription)
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            let error = error as NSError
            if error.code == NSURLErrorCancelled { return }
            console?.failed(error.localizedDescription)
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            // iOS ended the page's process (memory): bring the console back.
            webView.reload()
        }

        // Links that open a new window (target=_blank): Safari for other sites, here otherwise.
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView?
        {
            if let url = navigationAction.request.url {
                if let origin, Self.sameOrigin(url, origin) {
                    webView.load(navigationAction.request)
                } else {
                    UIApplication.shared.open(url)
                }
            }
            return nil
        }

        func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo) async
        {
            await Dialogs.alert(message)
        }

        func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo) async -> Bool
        {
            await Dialogs.confirm(message)
        }

        func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                     defaultText: String?, initiatedByFrame frame: WKFrameInfo) async -> String?
        {
            await Dialogs.prompt(prompt, defaultText: defaultText)
        }

        static func sameOrigin(_ a: URL, _ b: URL) -> Bool {
            a.scheme == b.scheme && a.host() == b.host() && (a.port ?? 0) == (b.port ?? 0)
        }

        static func unique(_ url: URL) -> URL {
            let fm = FileManager.default
            guard fm.fileExists(atPath: url.path(percentEncoded: false)) else { return url }
            let base = url.deletingPathExtension().lastPathComponent
            let ext = url.pathExtension
            for n in 2... {
                let candidate = url.deletingLastPathComponent()
                    .appending(path: ext.isEmpty ? "\(base) \(n)" : "\(base) \(n).\(ext)")
                if !fm.fileExists(atPath: candidate.path(percentEncoded: false)) { return candidate }
            }
            return url
        }
    }
}

/// JavaScript's alert, confirm and prompt, as UIKit alerts over the frontmost window.
@MainActor
enum Dialogs {
    static func alert(_ message: String) async {
        await withCheckedContinuation { continuation in
            let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: String(localized: "OK"), style: .default) { _ in continuation.resume() })
            present(alert) { continuation.resume() }
        }
    }

    static func confirm(_ message: String) async -> Bool {
        await withCheckedContinuation { continuation in
            let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: String(localized: "Cancel"), style: .cancel) { _ in
                continuation.resume(returning: false)
            })
            alert.addAction(UIAlertAction(title: String(localized: "OK"), style: .default) { _ in
                continuation.resume(returning: true)
            })
            present(alert) { continuation.resume(returning: false) }
        }
    }

    static func prompt(_ message: String, defaultText: String?) async -> String? {
        await withCheckedContinuation { continuation in
            let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            alert.addTextField { $0.text = defaultText }
            alert.addAction(UIAlertAction(title: String(localized: "Cancel"), style: .cancel) { _ in
                continuation.resume(returning: nil)
            })
            alert.addAction(UIAlertAction(title: String(localized: "OK"), style: .default) { [weak alert] _ in
                continuation.resume(returning: alert?.textFields?.first?.text ?? "")
            })
            present(alert) { continuation.resume(returning: nil) }
        }
    }

    /// Presents over the topmost view controller; `unavailable` when there is none.
    private static func present(_ controller: UIViewController, unavailable: () -> Void) {
        let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
        let window = (scenes.first { $0.activationState == .foregroundActive } ?? scenes.first)?.keyWindow
        var top = window?.rootViewController
        while let presented = top?.presentedViewController { top = presented }
        guard let top else { return unavailable() }
        top.present(controller, animated: true)
    }
}
