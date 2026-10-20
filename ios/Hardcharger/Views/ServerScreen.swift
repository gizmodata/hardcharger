import SwiftUI

/// The on-device server: its state, address, uptime and versions; start, stop, restart;
/// the database; sharing.
struct ServerScreen: View {
    @Environment(ServerController.self) private var server
    @Environment(SceneModel.self) private var scene

    var body: some View {
        NavigationStack {
            List {
                // A header, not a row: a row fires every button in it on one tap.
                Section {
                } header: {
                    hero
                        .textCase(nil)
                        .foregroundStyle(.primary)
                        .padding(.horizontal, -16)
                }
                if server.needsRestart {
                    Section {
                        Label {
                            VStack(alignment: .leading, spacing: 6) {
                                Text("Restart to apply your changes")
                                    .font(.subheadline.weight(.semibold))
                                Button("Restart Now") { Task { await server.restart() } }
                                    .hcProminentButton()
                                    .controlSize(.small)
                            }
                        } icon: {
                            Image(systemName: "arrow.triangle.2.circlepath")
                                .foregroundStyle(.orange)
                        }
                    }
                }
                if let message = server.phase.failureMessage {
                    Section("Why it stopped") {
                        Text(message)
                            .font(.callout.monospaced())
                            .foregroundStyle(.red)
                            .textSelection(.enabled)
                        if message.localizedCaseInsensitiveContains("in use") {
                            Button("Use Another Port") {
                                server.pickFreePort()
                                Task { await server.start() }
                            }
                        }
                    }
                }
                Section("Address") {
                    if let endpoint = server.localEndpoint {
                        CopyableRow(title: "On this device", value: endpoint.consoleURL.absoluteString, monospaced: true)
                    } else {
                        LabeledContent("Port", value: String(server.settings.port))
                    }
                    Button {
                        scene.showShare = true
                    } label: {
                        LabeledContent {
                            Text(server.runningSettings?.shareOnLocalNetwork == true ? "On" : "Off")
                        } label: {
                            Label("Share on Local Network", systemImage: "dot.radiowaves.left.and.right")
                        }
                    }
                    .tint(.primary)
                }
                Section("Database") {
                    LabeledContent("File") {
                        Text(verbatim: server.paths.database.lastPathComponent)
                            .font(.callout.monospaced())
                    }
                    TimelineView(.periodic(from: .now, by: 5)) { _ in
                        LabeledContent("Size", value: formattedBytes(server.databaseSize()))
                    }
                    Button {
                        scene.showImporter = true
                    } label: {
                        Label("Import CSV, JSON or Parquet…", systemImage: "square.and.arrow.down.on.square")
                    }
                    .disabled(server.phase != .running)
                    Text("Files › On My \(UIDevice.current.localizedModel) › Hardcharger holds the database, your imports and the console's downloads.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                Section("Resources") {
                    LabeledContent("Memory limit",
                                   value: "\(server.settings.memoryLimit.megabytes()) MB")
                    LabeledContent("Threads", value: server.settings.threads == 0
                        ? String(localized: "One per core (\(ProcessInfo.processInfo.activeProcessorCount))")
                        : String(server.settings.threads))
                    TimelineView(.periodic(from: .now, by: 2)) { _ in
                        LabeledContent("App memory in use", value: formattedBytes(appMemoryFootprint()))
                    }
                }
                Section {
                    NavigationLink {
                        LogsView()
                    } label: {
                        Label("Server Log", systemImage: "text.line.first.and.arrowtriangle.forward")
                    }
                    .accessibilityIdentifier("server.logs")
                }
                Section("Versions") {
                    LabeledContent("Hardcharger™", value: server.versions.hardcharger)
                    LabeledContent("DuckDB", value: server.versions.duckdb)
                    LabeledContent("Warp SQL", value: server.versions.warpSQL)
                    LabeledContent("Web UI", value: server.versions.webUI)
                }
            }
            .navigationTitle("Server")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    startStopButton
                }
            }
        }
    }

    private var hero: some View {
        VStack(spacing: 16) {
            HStack(alignment: .center, spacing: 16) {
                Image("HeadMark")
                    .resizable()
                    .scaledToFit()
                    .frame(width: 64, height: 64)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 6) {
                    Text("Hardcharger™")
                        .font(.title2.weight(.bold))
                    Text("on this \(UIDevice.current.localizedModel)")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
                StatusBadge(phase: server.phase)
            }
            if server.phase == .running, let startedAt = server.startedAt {
                HStack {
                    Metric(title: "Port", value: server.port.map(String.init) ?? "–")
                    Divider().frame(height: 32)
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        Metric(title: "Up", value: Self.uptime(from: startedAt, to: context.date))
                    }
                    Divider().frame(height: 32)
                    Metric(title: "TLS", value: server.usesTLS ? String(localized: "On") : String(localized: "Off"))
                }
                .frame(maxWidth: .infinity)
            }
            HStack(spacing: 12) {
                startStopButton
                    .hcProminentButton()
                    .controlSize(.large)
                if server.phase == .running {
                    Button {
                        Task { await server.restart() }
                    } label: {
                        Label("Restart", systemImage: "arrow.clockwise")
                    }
                    .hcButton()
                    .controlSize(.large)
                    .accessibilityIdentifier("server.restart")
                    Button {
                        scene.openWeb(.local)
                    } label: {
                        Label("Web UI", systemImage: "macwindow")
                    }
                    .hcButton()
                    .controlSize(.large)
                }
            }
        }
        .padding(20)
        .hcGlass(cornerRadius: 28)
        .padding(.vertical, 4)
    }

    @ViewBuilder private var startStopButton: some View {
        switch server.phase {
        case .running:
            Button {
                Task { await server.stop() }
            } label: {
                Label("Stop", systemImage: "stop.fill")
            }
            .accessibilityIdentifier("server.stop")
        case .starting, .stopping:
            Button {} label: {
                Label(server.phase == .starting ? "Starting" : "Stopping", systemImage: "hourglass")
            }
            .disabled(true)
        case .stopped, .failed:
            Button {
                Task { await server.start() }
            } label: {
                Label("Start", systemImage: "play.fill")
            }
            .accessibilityIdentifier("server.start")
        }
    }

    static func uptime(from start: Date, to now: Date) -> String {
        let seconds = max(0, Int(now.timeIntervalSince(start)))
        let formatter = DateComponentsFormatter()
        formatter.unitsStyle = .abbreviated
        formatter.allowedUnits = seconds >= 3600 ? [.day, .hour, .minute] : [.minute, .second]
        formatter.maximumUnitCount = 2
        return formatter.string(from: TimeInterval(seconds)) ?? "\(seconds)s"
    }
}

private struct Metric: View {
    let title: LocalizedStringKey
    let value: String

    var body: some View {
        VStack(spacing: 2) {
            Text(value)
                .font(.headline.monospacedDigit())
                .contentTransition(.numericText())
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .accessibilityElement(children: .combine)
    }
}
