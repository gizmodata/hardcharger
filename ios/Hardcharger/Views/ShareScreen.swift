import SwiftUI

/// Share on Local Network: the server listens on every interface with TLS (a certificate the
/// device made), so a computer on the same network opens the console at the address (or the
/// QR code) shown here and signs in with the administrator's password. Off by default.
struct ShareScreen: View {
    @Environment(ServerController.self) private var server
    @Environment(\.dismiss) private var dismiss
    @State private var confirmOn = false

    var body: some View {
        @Bindable var server = server
        List {
            Section {
                Toggle(isOn: Binding(get: { server.settings.shareOnLocalNetwork }, set: { on in
                    if on { confirmOn = true } else { turnOff() }
                })) {
                    Label("Share on Local Network", systemImage: "dot.radiowaves.left.and.right")
                }
                .accessibilityIdentifier("share.toggle")
            } footer: {
                Text("Anyone on this network who has the administrator password can read and change every table on this device. Turn it on only on networks you trust, and off when you are done.")
            }

            if server.settings.shareOnLocalNetwork {
                if server.phase == .running, server.runningSettings?.shareOnLocalNetwork == true {
                    sharing
                } else if server.phase.isBusy {
                    Section { ProgressView("Restarting the server…") }
                } else {
                    Section {
                        Button("Start Sharing") { Task { await server.restart() } }
                    }
                }
            }
        }
        .navigationTitle("Share on Local Network")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button("Done") { dismiss() }
            }
        }
        .confirmationDialog("Share this database on your network?", isPresented: $confirmOn, titleVisibility: .visible) {
            Button("Share") { turnOn() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Hardcharger restarts and accepts connections from other devices here, over TLS, for anyone with the administrator password. iOS asks for permission to use the local network.")
        }
    }

    @ViewBuilder private var sharing: some View {
        let urls = server.sharedConsoleURLs
        if urls.isEmpty {
            Section {
                Label("This device isn't on a Wi-Fi or wired network.", systemImage: "wifi.slash")
                    .foregroundStyle(.secondary)
            }
        }
        ForEach(urls, id: \.self) { url in
            Section {
                VStack(spacing: 14) {
                    QRCodeView(text: url.absoluteString)
                        .frame(width: 220, height: 220)
                    Text(verbatim: url.absoluteString)
                        .font(.title3.monospaced().weight(.semibold))
                        .multilineTextAlignment(.center)
                        .textSelection(.enabled)
                        .accessibilityIdentifier("share.url")
                    HStack {
                        Button {
                            UIPasteboard.general.url = url
                        } label: {
                            Label("Copy", systemImage: "doc.on.doc")
                        }
                        .hcButton()
                        ShareLink(item: url) {
                            Label("Share", systemImage: "square.and.arrow.up")
                        }
                        .hcButton()
                    }
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 8)
            } header: {
                Text("Open on a computer on this network")
            }
        }
        if let certificate = server.certificate {
            Section {
                Text(verbatim: certificate.fingerprint)
                    .font(.footnote.monospaced())
                    .textSelection(.enabled)
                    .accessibilityIdentifier("share.fingerprint")
            } header: {
                Text("Certificate fingerprint (SHA-256)")
            } footer: {
                Text("The browser warns that the certificate isn't from an authority it knows: this device made it. Check that the fingerprint the browser shows matches this one before you go on.")
            }
        }
        if let credential = try? server.credential() {
            Section("Sign in as") {
                LabeledContent("User name", value: credential.username)
                CopyableRow(title: "Password", value: credential.password, monospaced: true, secret: true)
            }
        }
        Section {
            Toggle("Keep the screen on while sharing", isOn: Binding(
                get: { server.settings.keepAwakeWhileSharing },
                set: { server.settings.keepAwakeWhileSharing = $0 }))
        } footer: {
            Text("iOS pauses apps that leave the screen; while paused, Hardcharger can't answer.")
        }
    }

    private func turnOn() {
        server.settings.shareOnLocalNetwork = true
        Task { await server.phase == .running ? server.restart() : server.start() }
    }

    private func turnOff() {
        server.settings.shareOnLocalNetwork = false
        if server.phase == .running { Task { await server.restart() } }
    }
}
