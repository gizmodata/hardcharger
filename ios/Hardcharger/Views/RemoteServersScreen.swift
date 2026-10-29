import SwiftUI

/// The Servers tab.
struct RemoteServersScreen: View {
    var body: some View {
        NavigationStack { RemoteServersView() }
    }
}

/// Other Hardcharger servers: saved connections (in the Keychain), each opening its web UI or
/// the console here.
struct RemoteServersView: View {
    @Environment(AppModel.self) private var model
    @Environment(RemoteServerStore.self) private var remotes
    @Environment(SceneModel.self) private var scene
    @State private var editing: RemoteServer?
    @State private var error: String?

    var body: some View {
            List {
                Section {
                    Button {
                        scene.openWeb(.local)
                    } label: {
                        RemoteRow(name: String(localized: "This \(UIDevice.current.localizedModel)"),
                                  detail: String(localized: "Hardcharger on this device"), systemImage: "iphone")
                    }
                    .tint(.primary)
                }
                Section {
                    ForEach(remotes.servers) { remote in
                        Button {
                            scene.openWeb(.remote(remote.id))
                        } label: {
                            RemoteRow(name: remote.name, detail: remote.displayHost,
                                      systemImage: remote.signIn == .browser ? "person.badge.key" : "network",
                                      endpoint: remote.endpoint)
                        }
                        .tint(.primary)
                        .contextMenu {
                            Button("Open Web UI", systemImage: "macwindow") { scene.openWeb(.remote(remote.id)) }
                            Button("Open in Console", systemImage: "terminal") { scene.openConsole(.remote(remote.id)) }
                            Button("Edit", systemImage: "pencil") { editing = remote }
                            Button("Delete", systemImage: "trash", role: .destructive) { delete(remote) }
                        }
                        .swipeActions {
                            Button("Delete", systemImage: "trash", role: .destructive) { delete(remote) }
                            Button("Edit", systemImage: "pencil") { editing = remote }
                        }
                        .swipeActions(edge: .leading) {
                            Button("Console", systemImage: "terminal") { scene.openConsole(.remote(remote.id)) }
                                .tint(.indigo)
                        }
                    }
                } header: {
                    Text("Saved servers")
                } footer: {
                    if remotes.servers.isEmpty {
                        Text("Add a Hardcharger server to open its web UI and console here: a server on your network, in your cloud, or another iPhone or iPad sharing its own.")
                    }
                }
            }
            .navigationTitle("Servers")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        editing = RemoteServer(name: "", endpoint: ChargewireEndpoint(origin: URL(string: "https://localhost")!))
                    } label: {
                        Label("Add Server", systemImage: "plus")
                    }
                    .keyboardShortcut("n", modifiers: .command)
                    .accessibilityIdentifier("servers.add")
                }
            }
            .sheet(item: $editing) { remote in
                RemoteServerEditor(original: remote, isNew: remotes.server(remote.id) == nil)
            }
            .onChange(of: model.pendingConnection, initial: true) { _, connection in
                if let connection {
                    editing = connection
                    model.pendingConnection = nil
                }
            }
            .alert("Couldn't delete", isPresented: Binding(get: { error != nil }, set: { if !$0 { error = nil } })) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(error ?? "")
            }
    }

    private func delete(_ remote: RemoteServer) {
        do {
            try remotes.delete(remote)
            if scene.webTarget == .remote(remote.id) { scene.webTarget = .local }
            if scene.sqlTarget == .remote(remote.id) { scene.sqlTarget = .local }
        } catch {
            self.error = error.localizedDescription
        }
    }
}

private struct RemoteRow: View {
    let name: String
    let detail: String
    let systemImage: String
    var endpoint: ChargewireEndpoint?
    @State private var reachable: Bool?

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: systemImage)
                .font(.title3)
                .frame(width: 32)
                .foregroundStyle(.tint)
            VStack(alignment: .leading, spacing: 2) {
                Text(name).font(.headline)
                Text(detail).font(.subheadline.monospaced()).foregroundStyle(.secondary)
            }
            Spacer()
            if let reachable {
                Image(systemName: reachable ? "checkmark.circle.fill" : "exclamationmark.circle")
                    .foregroundStyle(reachable ? .green : .orange)
                    .accessibilityLabel(reachable ? Text("Reachable") : Text("Not reachable"))
            }
            Image(systemName: "chevron.right")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(.tertiary)
        }
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .task(id: endpoint) {
            guard let endpoint else { return }
            reachable = await ChargewireClient(endpoint: endpoint, timeout: 5).isServing()
        }
    }
}

/// Adds or edits a saved server: its address, name and sign-in; "Test" asks the server what
/// it offers (and, for a self-signed certificate, offers to trust that one certificate).
struct RemoteServerEditor: View {
    let original: RemoteServer
    let isNew: Bool
    @Environment(RemoteServerStore.self) private var remotes
    @Environment(\.dismiss) private var dismiss
    @State private var draft: RemoteServer
    @State private var address: String
    @State private var testing = false
    @State private var discovery: Discovery?
    @State private var problem: String?
    @State private var untrustedFingerprint: String?
    @State private var untrustedCertificate: Data?

    init(original: RemoteServer, isNew: Bool) {
        self.original = original
        self.isNew = isNew
        _draft = State(initialValue: original)
        _address = State(initialValue: isNew && original.name.isEmpty ? "" : original.endpoint.origin.absoluteString)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("db.example.com:31337", text: $address)
                        .keyboardType(.URL)
                        .textContentType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .accessibilityIdentifier("editor.address")
                    TextField("Name", text: $draft.name)
                        .accessibilityIdentifier("editor.name")
                } header: {
                    Text("Server")
                } footer: {
                    Text("Its address and port (31337 unless it says otherwise). https unless it is on your local network.")
                }
                Section("Sign in") {
                    Picker("Sign in with", selection: $draft.signIn) {
                        ForEach(RemoteServer.SignIn.allCases) { Text($0.label).tag($0) }
                    }
                    if draft.signIn == .password {
                        TextField("User name", text: $draft.username)
                            .textContentType(.username)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .accessibilityIdentifier("editor.username")
                        SecureField("Password", text: $draft.password)
                            .textContentType(.password)
                            .accessibilityIdentifier("editor.password")
                    } else if let provider = discovery?.browserSignInProvider ?? draft.browserProvider {
                        Label("Sign in with \(provider) in a browser sheet", systemImage: "person.badge.key")
                            .foregroundStyle(.secondary)
                    }
                }
                Section {
                    Button {
                        Task { await test() }
                    } label: {
                        HStack {
                            Text("Test Connection")
                            Spacer()
                            if testing { ProgressView() }
                        }
                    }
                    .disabled(testing || RemoteServer.origin(from: address) == nil)
                    .accessibilityIdentifier("editor.test")
                    if let discovery {
                        Label("\(discovery.serverName) \(discovery.serverVersion)", systemImage: "checkmark.seal.fill")
                            .foregroundStyle(.green)
                        if draft.signIn == .browser, !discovery.offersBrowserSignIn {
                            Label("This server doesn't offer single sign-on.", systemImage: "exclamationmark.triangle")
                                .foregroundStyle(.orange)
                        }
                    }
                    if let problem {
                        Label(problem, systemImage: "xmark.octagon")
                            .foregroundStyle(.red)
                    }
                    if let fingerprint = untrustedFingerprint {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Its certificate isn't from an authority this device trusts. Trust only this certificate if its fingerprint matches the server's:")
                                .font(.footnote)
                            Text(verbatim: fingerprint).font(.caption.monospaced()).textSelection(.enabled)
                            Button("Trust This Certificate") {
                                draft.endpoint.pinnedFingerprint = fingerprint
                                draft.trustedCertificatePEM = untrustedCertificate.map {
                                    SelfSignedCertificate.pem($0, label: "CERTIFICATE")
                                }
                                untrustedFingerprint = nil
                                Task { await test() }
                            }
                        }
                    }
                    if let pinned = draft.endpoint.pinnedFingerprint {
                        LabeledContent("Trusted certificate") {
                            Text(verbatim: String(pinned.prefix(23)) + "…").font(.caption.monospaced())
                        }
                    }
                }
            }
            .navigationTitle(isNew ? "Add Server" : "Edit Server")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") { save() }
                        .disabled(RemoteServer.origin(from: address) == nil)
                        .accessibilityIdentifier("editor.save")
                }
            }
        }
    }

    private func applyAddress() -> Bool {
        guard let origin = RemoteServer.origin(from: address) else { return false }
        if origin != draft.endpoint.origin {
            if draft.endpoint.origin.host() != origin.host() {
                draft.endpoint.pinnedFingerprint = nil
                draft.trustedCertificatePEM = nil
            }
            draft.endpoint.origin = origin
        }
        return true
    }

    private func test() async {
        guard applyAddress() else { return }
        testing = true
        defer { testing = false }
        problem = nil
        discovery = nil
        let client = ChargewireClient(endpoint: draft.endpoint, timeout: 10)
        do {
            let found = try await client.discovery()
            discovery = found
            draft.serverVersion = found.serverVersion
            draft.browserProvider = found.browserSignInProvider
            if draft.name.isEmpty { draft.name = draft.endpoint.origin.host() ?? found.serverName }
            // A pinned certificate (a link from another device's Share screen): keep the
            // certificate itself too, for the console (OpenSSL trusts certificates, not
            // fingerprints).
            if let pinned = draft.endpoint.pinnedFingerprint, draft.trustedCertificatePEM == nil,
               let der = client.presentedCertificate, SelfSignedCertificate.fingerprint(of: der) == pinned {
                draft.trustedCertificatePEM = SelfSignedCertificate.pem(der, label: "CERTIFICATE")
            }
            if draft.signIn == .password, !draft.username.isEmpty, !draft.password.isEmpty {
                _ = try await client.signIn(username: draft.username, password: draft.password)
            }
        } catch {
            problem = error.localizedDescription
            if draft.endpoint.origin.scheme == "https", draft.endpoint.pinnedFingerprint == nil,
               let fingerprint = client.presentedFingerprint {
                untrustedFingerprint = fingerprint
                untrustedCertificate = client.presentedCertificate
            }
        }
    }

    private func save() {
        guard applyAddress() else { return }
        if draft.name.trimmingCharacters(in: .whitespaces).isEmpty {
            draft.name = draft.endpoint.origin.host() ?? "Hardcharger"
        }
        do {
            try remotes.save(draft)
            dismiss()
        } catch {
            problem = error.localizedDescription
        }
    }
}
