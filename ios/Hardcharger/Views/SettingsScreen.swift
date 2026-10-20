import SwiftUI

/// Settings: the server (port, memory, threads, logging), security (TLS, the administrator
/// password, the certificate), sharing, data, and About.
struct SettingsScreen: View {
    @Environment(ServerController.self) private var server
    @Environment(SceneModel.self) private var scene
    @State private var portText = ""
    @State private var confirmPassword = false
    @State private var confirmCertificate = false
    @State private var confirmReset = false
    @State private var error: String?

    var body: some View {
        @Bindable var server = server
        NavigationStack {
            Form {
                if server.needsRestart {
                    Section {
                        Button {
                            Task { await server.restart() }
                        } label: {
                            Label("Restart the server to apply changes", systemImage: "arrow.triangle.2.circlepath")
                        }
                        .accessibilityIdentifier("settings.restart")
                    }
                }

                Section {
                    Toggle("Start when the app opens", isOn: $server.settings.startAutomatically)
                        .accessibilityIdentifier("settings.startAutomatically")
                    LabeledContent("Port") {
                        TextField("Port", text: $portText)
                            .keyboardType(.numberPad)
                            .multilineTextAlignment(.trailing)
                            .font(.body.monospacedDigit())
                            .frame(maxWidth: 110)
                            .onSubmit(applyPort)
                            .accessibilityIdentifier("settings.port")
                    }
                    Picker("Memory limit", selection: $server.settings.memoryLimit) {
                        ForEach(MemoryLimit.allCases) { limit in
                            if limit == .automatic {
                                Text("Automatic (\(limit.megabytes()) MB)").tag(limit)
                            } else {
                                Text(limit.label).tag(limit)
                            }
                        }
                    }
                    .accessibilityIdentifier("settings.memoryLimit")
                    Stepper(value: $server.settings.threads, in: 0...ProcessInfo.processInfo.activeProcessorCount) {
                        LabeledContent("Threads", value: server.settings.threads == 0
                            ? String(localized: "One per core")
                            : String(server.settings.threads))
                    }
                    .accessibilityIdentifier("settings.threads")
                } header: {
                    Text("Server")
                } footer: {
                    Text("iOS ends an app that uses too much memory, so the server keeps DuckDB well inside it: larger work spills to the device's storage.")
                }

                Section("Logging") {
                    Picker("Log level", selection: $server.settings.logLevel) {
                        ForEach(ServerLogLevel.allCases) { Text($0.label).tag($0) }
                    }
                    Toggle("Log every statement", isOn: $server.settings.printQueries)
                        .accessibilityIdentifier("settings.printQueries")
                    Picker("Log view", selection: $server.settings.logStyle) {
                        ForEach(LogStyle.allCases) { Text($0.label).tag($0) }
                    }
                }

                Section {
                    Button {
                        scene.showShare = true
                    } label: {
                        LabeledContent {
                            Text(server.settings.shareOnLocalNetwork ? "On" : "Off")
                        } label: {
                            Label("Share on Local Network", systemImage: "dot.radiowaves.left.and.right")
                        }
                    }
                    .tint(.primary)
                    .accessibilityIdentifier("settings.share")
                    Toggle("Use TLS on this device too", isOn: $server.settings.alwaysUseTLS)
                        .accessibilityIdentifier("settings.alwaysUseTLS")
                } header: {
                    Text("Network")
                } footer: {
                    Text("Sharing always uses TLS, with a certificate this device makes for itself.")
                }

                Section {
                    if let credential = try? server.credential() {
                        LabeledContent("User name", value: credential.username)
                        CopyableRow(title: "Password", value: credential.password, monospaced: true, secret: true)
                    }
                    Button("New Password…") { confirmPassword = true }
                    Button("New Certificate…") { confirmCertificate = true }
                } header: {
                    Text("Administrator")
                } footer: {
                    Text("Made on this device and kept in its Keychain. The console here signs in by itself; others need this password.")
                }

                Section("Data") {
                    LabeledContent("Database") {
                        Text(verbatim: "Hardcharger.duckdb").font(.callout.monospaced())
                    }
                    Button("Import Data…") { scene.showImporter = true }
                        .disabled(server.phase != .running)
                    Button("Erase Database…", role: .destructive) { confirmReset = true }
                }

                Section {
                    NavigationLink {
                        RemoteServersView()
                    } label: {
                        Label("Saved Servers", systemImage: "network")
                    }
                    .accessibilityIdentifier("settings.servers")
                    NavigationLink {
                        AboutScreen()
                    } label: {
                        Label("About Hardcharger™", systemImage: "info.circle")
                    }
                    .accessibilityIdentifier("settings.about")
                }
            }
            .navigationTitle("Settings")
            .onAppear { portText = String(server.settings.port) }
            .onChange(of: server.settings.port) { _, port in portText = String(port) }
            .toolbar {
                ToolbarItemGroup(placement: .keyboard) {
                    Spacer()
                    Button("Done", action: applyPort)
                }
            }
            .confirmationDialog("Make a new administrator password?", isPresented: $confirmPassword,
                                titleVisibility: .visible) {
                Button("New Password", role: .destructive) { run { try await server.regeneratePassword() } }
            } message: {
                Text("The server restarts. Anyone signed in elsewhere with the old password has to sign in again.")
            }
            .confirmationDialog("Make a new TLS certificate?", isPresented: $confirmCertificate,
                                titleVisibility: .visible) {
                Button("New Certificate", role: .destructive) { run { try await server.regenerateCertificate() } }
            } message: {
                Text("Its fingerprint changes: computers that trusted the old one will warn again.")
            }
            .confirmationDialog("Erase the database?", isPresented: $confirmReset, titleVisibility: .visible) {
                Button("Erase Database", role: .destructive) { run { try await eraseDatabase() } }
            } message: {
                Text("Every table on this device is deleted. Imported files stay in Files › Hardcharger › Imports.")
            }
            .alert("Something went wrong", isPresented: Binding(get: { error != nil }, set: { if !$0 { error = nil } })) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(error ?? "")
            }
        }
    }

    private func applyPort() {
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
        guard let port = Int(portText), ServerSettings.validPorts.contains(port) else {
            portText = String(server.settings.port)
            error = String(localized: "A port is a number from 1024 to 65535.")
            return
        }
        server.settings.port = port
    }

    private func eraseDatabase() async throws {
        let wasRunning = server.phase == .running
        await server.stop()
        let fm = FileManager.default
        for url in [server.paths.database, server.paths.database.appendingPathExtension("wal")]
            where fm.fileExists(atPath: url.path(percentEncoded: false)) {
            try fm.removeItem(at: url)
        }
        if wasRunning { await server.start() }
    }

    private func run(_ action: @escaping () async throws -> Void) {
        Task {
            do { try await action() } catch { self.error = error.localizedDescription }
        }
    }
}

/// About: versions, the licences of what the app contains, and the trademark notice.
struct AboutScreen: View {
    @Environment(ServerController.self) private var server

    var body: some View {
        List {
            Section {
                VStack(spacing: 18) {
                    Image("Mascot")
                        .resizable()
                        .scaledToFit()
                        .frame(maxWidth: 260)
                        .accessibilityLabel("Hardcharger™")
                    Text("The Hardcharger™ server, on your \(UIDevice.current.localizedModel): DuckDB, Arrow and the Warp SQL protocol, with its web console.")
                        .font(.callout)
                        .multilineTextAlignment(.center)
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 12)
                .listRowBackground(Color.clear)
            }
            Section("Versions") {
                LabeledContent("App", value: Self.appVersion)
                LabeledContent("Hardcharger™ server", value: server.versions.hardcharger)
                LabeledContent("DuckDB", value: server.versions.duckdb)
                LabeledContent("Warp SQL", value: server.versions.warpSQL)
                LabeledContent("Web UI", value: server.versions.webUI)
            }
            Section {
                NavigationLink("Third-Party Software") {
                    NoticesView()
                }
                Link(destination: URL(string: "https://gizmodata.com")!) {
                    Label("gizmodata.com", systemImage: "safari")
                }
            }
            Section {
                VStack(spacing: 14) {
                    ByGizmoData()
                    Text("© \(String(Calendar.current.component(.year, from: .now))) GizmoData LLC. All rights reserved.")
                        .font(.footnote)
                    Text("Hardcharger™ and GizmoData™ are trademarks of GizmoData LLC.")
                        .font(.footnote)
                        .accessibilityIdentifier("about.trademarks")
                }
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity)
                .listRowBackground(Color.clear)
            }
        }
        .navigationTitle("About")
        .navigationBarTitleDisplayMode(.inline)
    }

    static var appVersion: String {
        let info = Bundle.main.infoDictionary
        let version = info?["HardchargerVersion"] as? String ?? info?["CFBundleShortVersionString"] as? String ?? ""
        let build = info?["CFBundleVersion"] as? String ?? ""
        return "\(version) (\(build))"
    }
}

/// THIRD_PARTY_NOTICES.md as built into the server, then the web console's own notices.
private struct NoticesView: View {
    @State private var text = ""

    var body: some View {
        ScrollView {
            Text(text)
                .font(.caption.monospaced())
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding()
        }
        .navigationTitle("Third-Party Software")
        .navigationBarTitleDisplayMode(.inline)
        .task {
            var notices = EmbeddedServer.thirdPartyNotices
            if let url = Bundle.main.url(forResource: "AppThirdPartyNotices", withExtension: "md"),
               let app = try? String(contentsOf: url, encoding: .utf8) {
                notices = app + "\n\n" + notices
            }
            text = notices
        }
    }
}
