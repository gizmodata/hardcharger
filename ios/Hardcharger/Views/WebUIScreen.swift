import SwiftUI
import WebKit

/// The web UI: Hardcharger's own (query editor, sessions, API, uploads) for this device's
/// server or a remote one, signed in by the app.
struct WebUIScreen: View {
    @Environment(AppModel.self) private var model
    @Environment(ServerController.self) private var server
    @Environment(RemoteServerStore.self) private var remotes
    @Environment(SceneModel.self) private var scene
    @State private var signInError: String?
    @State private var presentation = WindowPresentation()
    @State private var keyboardShown = false

    var body: some View {
        NavigationStack {
            content
                .navigationTitle(title)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar { toolbar }
                .toolbarRole(.editor)
                .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)) { _ in
                    keyboardShown = true
                }
                .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillHideNotification)) { _ in
                    keyboardShown = false
                }
        }
    }

    private var title: String {
        switch scene.webTarget {
        case .local: String(localized: "Web UI")
        case .remote(let id): remotes.server(id)?.name ?? String(localized: "Web UI")
        }
    }

    @ViewBuilder private var content: some View {
        switch scene.webTarget {
        case .local:
            localContent
        case .remote(let id):
            if let remote = remotes.server(id) {
                RemoteConsole(remote: remote, presentation: presentation)
            } else {
                ContentUnavailableView("Server not found", systemImage: "questionmark.circle",
                                       description: Text("It was deleted. Pick another server."))
            }
        }
    }

    @ViewBuilder private var localContent: some View {
        switch server.phase {
        case .running:
            ConsoleWebView(console: scene.webConsole(for: .local))
                .task(id: server.startedAt) { await loadLocal() }
        case .starting:
            ProgressView("Starting the server…")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .stopping:
            ProgressView("Stopping…")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .stopped, .failed:
            ContentUnavailableView {
                Label("The server is stopped", systemImage: "bolt.slash")
            } description: {
                if let message = server.phase.failureMessage {
                    Text(message)
                } else {
                    Text("Start Hardcharger on this device to use its web UI.")
                }
            } actions: {
                Button {
                    Task { await server.start() }
                } label: {
                    Label("Start Server", systemImage: "play.fill")
                }
                .hcProminentButton()
                .accessibilityIdentifier("console.start")
                if let message = server.phase.failureMessage, message.localizedCaseInsensitiveContains("in use") {
                    Button("Use Another Port") {
                        server.pickFreePort()
                        Task { await server.start() }
                    }
                }
            }
        }
    }

    private func loadLocal() async {
        let console = scene.webConsole(for: .local)
        guard let endpoint = server.localEndpoint else { return }
        do {
            let token = try await server.accessToken()
            console.load(endpoint: endpoint, token: token)
        } catch {
            console.load(endpoint: endpoint, token: nil)
        }
    }

    @ToolbarContentBuilder private var toolbar: some ToolbarContent {
        ToolbarItem(placement: .topBarLeading) {
            Menu {
                Picker("Server", selection: Binding(get: { scene.webTarget }, set: { scene.webTarget = $0 })) {
                    Label("This Device", systemImage: "iphone").tag(ConsoleTarget.local)
                    ForEach(remotes.servers) { remote in
                        Label(remote.name, systemImage: "network").tag(ConsoleTarget.remote(remote.id))
                    }
                }
                Divider()
                Button {
                    scene.tab = .servers
                } label: {
                    Label("Add a Server…", systemImage: "plus")
                }
            } label: {
                Label("Server", systemImage: "server.rack")
            }
            .accessibilityIdentifier("console.target")
        }
        ToolbarItemGroup(placement: .topBarTrailing) {
            // On a phone the keyboard covers the editor's results.
            if keyboardShown {
                Button {
                    let webView = scene.webConsole(for: scene.webTarget).webView
                    // The page's focus (the editor's hidden input) first, then the view's.
                    webView.evaluateJavaScript(
                        "document.querySelectorAll('textarea, input').forEach(e => e.blur()); document.activeElement && document.activeElement.blur && document.activeElement.blur()"
                    ) { _, _ in
                        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil,
                                                        for: nil)
                    }
                } label: {
                    Label("Hide Keyboard", systemImage: "keyboard.chevron.compact.down")
                }
                .accessibilityIdentifier("webui.hideKeyboard")
            }
            if scene.webTarget == .local {
                Button {
                    scene.showImporter = true
                } label: {
                    Label("Import Data", systemImage: "square.and.arrow.down.on.square")
                }
                .disabled(server.phase != .running)
                .accessibilityIdentifier("console.import")
                Button {
                    scene.showShare = true
                } label: {
                    Label("Share on Local Network", systemImage: "dot.radiowaves.left.and.right")
                }
            }
            Button {
                Task { await reload() }
            } label: {
                Label("Reload", systemImage: "arrow.clockwise")
            }
            .keyboardShortcut("r", modifiers: .command)
            .accessibilityIdentifier("console.reload")
        }
    }

    /// Reload with a fresh sign-in (a console signed out, or a token about to expire).
    private func reload() async {
        switch scene.webTarget {
        case .local:
            await loadLocal()
        case .remote(let id):
            remotes.forgetToken(for: id)
            // RemoteConsole's task signs in again.
            scene.remoteReloads += 1
        }
    }
}

/// A remote server's console: signs in (password, or browser sign-in), then loads.
private struct RemoteConsole: View {
    let remote: RemoteServer
    let presentation: WindowPresentation
    @Environment(RemoteServerStore.self) private var remotes
    @Environment(SceneModel.self) private var scene
    @State private var error: String?
    @State private var signingIn = false

    var body: some View {
        ZStack {
            ConsoleWebView(console: scene.webConsole(for: .remote(remote.id)))
            if signingIn {
                ProgressView(remote.signIn == .browser ? "Signing in with your browser…" : "Signing in…")
                    .padding(24)
                    .hcGlass(cornerRadius: 20)
            } else if let error {
                ContentUnavailableView {
                    Label("Couldn't sign in", systemImage: "person.crop.circle.badge.exclamationmark")
                } description: {
                    Text(error)
                } actions: {
                    Button("Try Again") { Task { await signIn() } }
                        .hcProminentButton()
                }
                .background(.background)
            }
        }
        .task(id: RemoteLoad(remote: remote, reloads: scene.remoteReloads)) { await signIn() }
    }

    private func signIn() async {
        error = nil
        let console = scene.webConsole(for: .remote(remote.id))
        if remotes.token(for: remote.id) != nil, console.loadedOrigin == remote.endpoint.origin {
            return  // signed in and loaded already
        }
        signingIn = true
        defer { signingIn = false }
        let client = WarpClient(endpoint: remote.endpoint)
        do {
            let token: AccessToken
            switch remote.signIn {
            case .password:
                token = try await client.signIn(username: remote.username, password: remote.password)
            case .browser:
                token = try await BrowserSignIn.signIn(client: client, presentation: presentation)
            }
            remotes.remember(token, for: remote.id)
            console.load(endpoint: remote.endpoint, token: token)
        } catch is CancellationError {
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private struct RemoteLoad: Hashable {
    let remote: RemoteServer
    let reloads: Int
}

/// The console's web view, with a progress bar and its errors.
struct ConsoleWebView: View {
    let console: WebConsole
    @State private var showDownload = false

    var body: some View {
        WebViewHost(webView: console.webView)
            .overlay(alignment: .top) {
                if console.isLoading {
                    ProgressView(value: console.progress)
                        .progressViewStyle(.linear)
                        .tint(.accentColor)
                        .transition(.opacity)
                }
            }
            .overlay {
                if let message = console.errorMessage {
                    ContentUnavailableView {
                        Label("The console didn't load", systemImage: "exclamationmark.triangle")
                    } description: {
                        Text(message)
                    } actions: {
                        Button("Reload") { console.reload() }
                            .hcProminentButton()
                    }
                    .background(.background)
                }
            }
            .overlay(alignment: .bottom) {
                if let file = console.lastDownload {
                    DownloadToast(file: file) { console.lastDownload = nil }
                        .padding()
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                }
            }
            .animation(.default, value: console.lastDownload)
            .animation(.default, value: console.isLoading)
    }
}

private struct DownloadToast: View {
    let file: URL
    let dismiss: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "arrow.down.doc.fill")
                .font(.title2)
                .foregroundStyle(.tint)
            VStack(alignment: .leading) {
                Text("Saved \(file.lastPathComponent)")
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(1)
                Text("Files › Hardcharger › Exports")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            ShareLink(item: file) {
                Image(systemName: "square.and.arrow.up")
            }
            Button(action: dismiss) {
                Image(systemName: "xmark")
            }
            .accessibilityLabel(Text("Dismiss"))
        }
        .padding(14)
        .hcGlass(cornerRadius: 18)
        .task {
            try? await Task.sleep(for: .seconds(8))
            dismiss()
        }
    }
}

/// Hosts a WKWebView that outlives the view (the console keeps its page across tabs).
private struct WebViewHost: UIViewRepresentable {
    let webView: WKWebView

    func makeUIView(context: Context) -> UIView {
        let container = UIView()
        container.backgroundColor = .systemBackground
        attach(to: container)
        return container
    }

    func updateUIView(_ container: UIView, context: Context) {
        if webView.superview !== container { attach(to: container) }
    }

    private func attach(to container: UIView) {
        webView.removeFromSuperview()
        webView.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(webView)
        // Inside the safe area: the page's own bars (the editor's Run row) end above the
        // floating tab bar instead of under it.
        let guide = container.safeAreaLayoutGuide
        NSLayoutConstraint.activate([
            webView.leadingAnchor.constraint(equalTo: guide.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: guide.trailingAnchor),
            webView.topAnchor.constraint(equalTo: guide.topAnchor),
            webView.bottomAnchor.constraint(equalTo: guide.bottomAnchor),
        ])
    }
}

/// What an import made.
struct ImportResultSheet: View {
    let results: [DataImport.Result]
    @Environment(\.dismiss) private var dismiss
    @Environment(SceneModel.self) private var scene

    var body: some View {
        NavigationStack {
            List(results) { result in
                VStack(alignment: .leading, spacing: 4) {
                    Label {
                        Text(verbatim: result.table)
                            .font(.headline.monospaced())
                    } icon: {
                        Image(systemName: "tablecells")
                            .foregroundStyle(.tint)
                    }
                    Group {
                        if let rows = result.rows {
                            Text("\(rows) rows from \(result.file)")
                        } else {
                            Text("From \(result.file)")
                        }
                    }
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                }
                .accessibilityElement(children: .combine)
            }
            .navigationTitle(results.count == 1 ? "Imported a Table" : "Imported \(results.count) Tables")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Open Web UI") {
                        scene.openWeb(.local)
                        dismiss()
                    }
                }
            }
        }
        .presentationDetents([.medium])
    }
}
