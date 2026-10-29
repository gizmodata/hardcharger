import SwiftUI

/// The SQL console: the `hardcharger` command-line client in the app, against this device's
/// server or a saved remote one.
struct SQLConsoleScreen: View {
    @Environment(ServerController.self) private var server
    @Environment(RemoteServerStore.self) private var remotes
    @Environment(SceneModel.self) private var scene
    @Environment(\.dynamicTypeSize) private var typeSize
    @State private var presentation = WindowPresentation()
    @FocusState private var inputFocused: Bool

    private var console: SQLConsole { scene.sqlConsole(for: scene.sqlTarget) }

    /// The console's type size: monospaced, following Dynamic Type.
    private var uiFont: UIFont {
        UIFontMetrics(forTextStyle: .footnote).scaledFont(
            for: .monospacedSystemFont(ofSize: 13, weight: .regular), compatibleWith: UITraitCollection(
                preferredContentSizeCategory: UIContentSizeCategory(typeSize)))
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                transcript
                Divider()
                inputArea
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { toolbar }
            .task(id: connectionKey) { await connect() }
            // Another tab: the input lets the keyboard go (it would stay up over that tab).
            .onDisappear { inputFocused = false }
        }
    }

    private var title: String {
        switch scene.sqlTarget {
        case .local: String(localized: "Console")
        case .remote(let id): remotes.server(id)?.name ?? String(localized: "Console")
        }
    }

    // MARK: - Transcript

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    banner
                    ForEach(console.entries) { entry in
                        EntryView(entry: entry, continuationPrompt: console.continuationPrompt, uiFont: uiFont)
                            .id(entry.id)
                    }
                    if console.isRunning {
                        HStack(spacing: 10) {
                            ProgressView().controlSize(.small)
                            Text("Running…").font(Font(uiFont)).foregroundStyle(.secondary)
                            Button("Cancel", role: .destructive) { console.cancel() }
                                .font(.footnote)
                                .accessibilityIdentifier("sqlconsole.cancel")
                        }
                        .id("running")
                    }
                    Color.clear.frame(height: 1).id("end")
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 12)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .defaultScrollAnchor(.bottom)
            .scrollDismissesKeyboard(.interactively)
            .onChange(of: console.entries) { _, _ in
                withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo("end", anchor: .bottom) }
            }
            .accessibilityIdentifier("sqlconsole.transcript")
        }
        .background(Color(uiColor: .secondarySystemBackground))
    }

    @ViewBuilder private var banner: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Hardcharger™ client \(server.versions.hardcharger)")
                .font(Font(uiFont).weight(.semibold))
            if console.isConnecting {
                Label("Connecting…", systemImage: "bolt.horizontal")
            } else if let error = console.connectionError {
                Text(error).foregroundStyle(.red)
                Button("Try Again") { Task { await connect(force: true) } }
            } else if console.isConnected {
                Text("Statements end with ';'. Type .help for the dot commands, ⌘↩ to run as written.")
                    .foregroundStyle(.secondary)
            } else if scene.sqlTarget == .local, server.phase != .running {
                Text("The server on this device isn't running.").foregroundStyle(.secondary)
                Button("Start Server") { Task { await server.start() } }
            }
        }
        .font(Font(uiFont))
    }

    // MARK: - Input

    private var inputArea: some View {
        VStack(spacing: 8) {
            HStack(alignment: .top, spacing: 0) {
                // A prompt per line: the catalog.schema prompt, then the continuation.
                VStack(alignment: .trailing, spacing: 0) {
                    ForEach(Array(promptLines.enumerated()), id: \.offset) { _, prompt in
                        Text(prompt)
                            .font(Font(uiFont))
                            .foregroundStyle(.tint)
                            .frame(height: uiFont.lineHeight)
                    }
                }
                .fixedSize()
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(Text("Catalog and schema"))
                .accessibilityValue(Text(verbatim: console.prompt.trimmingCharacters(in: CharacterSet(charactersIn: "> "))))
                .accessibilityIdentifier("sqlconsole.prompt")
                ConsoleInputView(
                    text: Binding(get: { console.input }, set: { console.input = $0 }),
                    font: uiFont,
                    active: scene.tab == .console,
                    onReturn: { console.returnPressed() },
                    onRun: { console.submit() },
                    onHistoryBack: { console.historyBack() },
                    onHistoryForward: { console.historyForward() },
                    onEscape: { console.clearInput() },
                    onCancel: { console.cancel() },
                    onClearTranscript: { console.clearTranscript() })
                .focused($inputFocused)
            }
            HStack(spacing: 10) {
                Button {
                    console.historyBack()
                } label: {
                    Image(systemName: "chevron.up")
                }
                .accessibilityLabel(Text("Previous"))
                Button {
                    console.historyForward()
                } label: {
                    Image(systemName: "chevron.down")
                }
                .accessibilityLabel(Text("Next"))
                Button {
                    console.clearInput()
                } label: {
                    Image(systemName: "xmark.circle")
                }
                .accessibilityLabel(Text("Clear input"))
                .accessibilityIdentifier("sqlconsole.clearInput")
                if inputFocused {
                    Button {
                        inputFocused = false
                    } label: {
                        Image(systemName: "keyboard.chevron.compact.down")
                    }
                    .accessibilityLabel(Text("Hide keyboard"))
                    .accessibilityIdentifier("sqlconsole.hideKeyboard")
                }
                Spacer()
                if console.isRunning {
                    Button(role: .destructive) {
                        console.cancel()
                    } label: {
                        Label("Cancel", systemImage: "stop.fill")
                    }
                    .hcButton()
                } else {
                    Button {
                        console.submit()
                    } label: {
                        Label("Run", systemImage: "play.fill")
                    }
                    .hcProminentButton()
                    .disabled(console.input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityIdentifier("sqlconsole.run")
                }
            }
            .buttonStyle(.borderless)
            .controlSize(.regular)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(.bar)
    }

    private var promptLines: [String] {
        let count = console.input.reduce(into: 1) { n, c in if c == "\n" { n += 1 } }
        return (0..<min(count, 10)).map { console.promptForLine($0) }
    }

    // MARK: - Toolbar

    @ToolbarContentBuilder private var toolbar: some ToolbarContent {
        ToolbarItem(placement: .topBarLeading) {
            Menu {
                Picker("Server", selection: Binding(get: { scene.sqlTarget }, set: { scene.sqlTarget = $0 })) {
                    Label("This Device", systemImage: "iphone").tag(ConsoleTarget.local)
                    ForEach(remotes.servers) { remote in
                        Label(remote.name, systemImage: "network").tag(ConsoleTarget.remote(remote.id))
                    }
                }
            } label: {
                Label("Server", systemImage: "server.rack")
            }
            .accessibilityIdentifier("sqlconsole.target")
        }
        ToolbarItemGroup(placement: .topBarTrailing) {
            Menu {
                ShareLink(item: console.transcript) {
                    Label("Share Transcript", systemImage: "square.and.arrow.up")
                }
                Button {
                    UIPasteboard.general.string = console.transcript
                } label: {
                    Label("Copy Transcript", systemImage: "doc.on.doc")
                }
                Button(role: .destructive) {
                    console.clearTranscript()
                } label: {
                    Label("Clear Transcript", systemImage: "trash")
                }
                Divider()
                Button {
                    Task { await connect(force: true) }
                } label: {
                    Label("Reconnect", systemImage: "arrow.clockwise")
                }
            } label: {
                Label("More", systemImage: "ellipsis.circle")
            }
        }
    }

    // MARK: - Connecting

    private var connectionKey: String {
        switch scene.sqlTarget {
        case .local:
            "local-\(server.phase == .running ? (server.startedAt?.timeIntervalSince1970 ?? 0) : -1)"
        case .remote(let id):
            "remote-\(id)-\(remotes.server(id)?.endpoint.origin.absoluteString ?? "")-\(scene.remoteReloads)"
        }
    }

    private func connect(force: Bool = false) async {
        let console = console
        if force { console.disconnect() }
        switch scene.sqlTarget {
        case .local:
            guard server.phase == .running, let port = server.port else {
                console.disconnect()
                return
            }
            let tls = server.usesTLS
            let certificate = server.paths.tlsCertificate.path(percentEncoded: false)
            let credential = try? server.credential()
            await console.connect(options: {
                ConsoleEngine.Options(host: "127.0.0.1", port: port, tls: tls, tlsRoots: tls ? certificate : nil,
                                      username: credential?.username, password: credential?.password)
            }, key: connectionKey)
        case .remote(let id):
            guard let remote = remotes.server(id) else { return }
            let remotes = remotes
            let presentation = presentation
            await console.connect(options: {
                try await ConsoleConnection.options(for: remote, remotes: remotes, presentation: presentation)
            }, key: connectionKey)
        }
    }
}

/// How the console reaches a remote server: its address, the certificates to trust (the one
/// the person trusted, else the app's CA bundle), and its sign-in.
@MainActor
enum ConsoleConnection {
    static func options(for remote: RemoteServer, remotes: RemoteServerStore,
                        presentation: WindowPresentation) async throws -> ConsoleEngine.Options
    {
        let origin = remote.endpoint.origin
        let tls = origin.scheme == "https"
        var options = ConsoleEngine.Options(host: origin.host(percentEncoded: false) ?? "localhost",
                                            port: origin.port ?? (tls ? 443 : 80), tls: tls)
        if tls {
            if let pem = remote.trustedCertificatePEM {
                let file = FileManager.default.temporaryDirectory.appending(path: "trusted-\(remote.id).pem")
                try Data(pem.utf8).write(to: file, options: .atomic)
                options.tlsRoots = file.path(percentEncoded: false)
            } else {
                options.tlsRoots = CABundle.path
            }
        }
        switch remote.signIn {
        case .password:
            options.username = remote.username
            options.password = remote.password
        case .browser:
            if let token = remotes.token(for: remote.id) {
                options.accessToken = token.token
            } else {
                let token = try await BrowserSignIn.signIn(client: ChargewireClient(endpoint: remote.endpoint),
                                                           presentation: presentation)
                remotes.remember(token, for: remote.id)
                options.accessToken = token.token
            }
        }
        return options
    }
}

/// The CA certificates the app bundles for OpenSSL (Resources/cacert.pem): iOS has no CA
/// bundle on disk. The server (outbound HTTPS) and the console find it through SSL_CERT_FILE.
enum CABundle {
    static var path: String? { Bundle.main.path(forResource: "cacert", ofType: "pem") }

    static func install() {
        if let path, getenv("SSL_CERT_FILE") == nil { setenv("SSL_CERT_FILE", path, 1) }
    }
}

/// One run in the transcript: the prompt and input, then the output, scrolling sideways when
/// a table is wider than the screen.
private struct EntryView: View {
    let entry: SQLConsole.Entry
    let continuationPrompt: String
    let uiFont: UIFont
    private var font: Font { Font(uiFont) }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(inputText)
                .font(font.weight(.medium))
                .foregroundStyle(.tint)
                .textSelection(.enabled)
            if !entry.output.isEmpty {
                ConsoleOutputView(text: entry.output.hasSuffix("\n") ? String(entry.output.dropLast()) : entry.output,
                                  font: uiFont, color: entry.kind == .error ? .systemRed : .label)
            }
            if let duration = entry.duration, duration > .milliseconds(250) {
                Text(duration.formatted(.units(allowed: [.seconds, .milliseconds], width: .narrow)))
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .contextMenu {
            Button {
                UIPasteboard.general.string = entry.output
            } label: {
                Label("Copy Output", systemImage: "doc.on.doc")
            }
            Button {
                UIPasteboard.general.string = entry.input
            } label: {
                Label("Copy Statement", systemImage: "text.cursor")
            }
            ShareLink(item: entry.output) {
                Label("Share Output", systemImage: "square.and.arrow.up")
            }
        }
    }

    private var inputText: String {
        entry.input.split(separator: "\n", omittingEmptySubsequences: false).enumerated()
            .map { ($0.offset == 0 ? entry.prompt : continuationPrompt) + $0.element }
            .joined(separator: "\n")
    }
}
