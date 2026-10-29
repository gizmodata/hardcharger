import SwiftUI

/// The on-device server: its state, address, uptime and versions; start, stop, restart;
/// the database; sharing.
struct ServerScreen: View {
    @Environment(ServerController.self) private var server
    @Environment(SceneModel.self) private var scene
    @Environment(\.dynamicTypeSize) private var typeSize

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
                Section {
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
                } header: {
                    Text("Address")
                } footer: {
                    if let note = server.portNote {
                        Label(note, systemImage: "info.circle")
                    }
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
                    LabeledContent("Chargewire", value: server.versions.chargewire)
                    LabeledContent("Web UI", value: server.versions.webUI)
                }
            }
            .navigationTitle("Server")
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
                        .lineLimit(1)
                        .minimumScaleFactor(0.4)
                    Text("on this \(UIDevice.current.localizedModel)")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    StatusBadge(phase: server.phase)
                }
                Spacer(minLength: 0)
            }
            if server.phase == .running, let startedAt = server.startedAt {
                // Side by side; one under the other at the accessibility text sizes.
                let layout = typeSize.isAccessibilitySize
                    ? AnyLayout(VStackLayout(alignment: .leading, spacing: 8)) : AnyLayout(HStackLayout())
                layout {
                    Metric(title: "Port", value: server.port.map(String.init) ?? "–", stacked: typeSize.isAccessibilitySize)
                    if !typeSize.isAccessibilitySize { Divider().frame(height: 32) }
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        Metric(title: "Up", value: Self.uptime(from: startedAt, to: context.date),
                               stacked: typeSize.isAccessibilitySize)
                    }
                    if !typeSize.isAccessibilitySize { Divider().frame(height: 32) }
                    Metric(title: "TLS", value: server.usesTLS ? String(localized: "On") : String(localized: "Off"),
                           stacked: typeSize.isAccessibilitySize)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            // Titles and icons where they fit, icons alone where they don't (narrow windows,
            // large text); VoiceOver reads the titles either way.
            ViewThatFits(in: .horizontal) {
                heroButtons.labelStyle(.titleAndIcon).controlSize(.large)
                stackedHeroButtons
            }
        }
        .padding(20)
        .hcGlass(cornerRadius: 28)
        .padding(.vertical, 4)
    }

    private var heroButtons: some View {
        HStack(spacing: 10) {
            startStopButton()
                .hcProminentButton()
            if server.phase == .running {
                Button {
                    Task { await server.restart() }
                } label: {
                    Label("Restart", systemImage: "arrow.clockwise")
                }
                .hcButton()
                .accessibilityIdentifier("server.restart")
                Button {
                    scene.openWeb(.local)
                } label: {
                    Label("Web UI", systemImage: "macwindow")
                }
                .hcButton()
            }
        }
        .lineLimit(1)
        .fixedSize()
    }

    /// Narrow screens: start/stop across the card, the others below it.
    private var stackedHeroButtons: some View {
        VStack(spacing: 10) {
            startStopButton(wide: true)
                .hcProminentButton()
            if server.phase == .running {
                HStack(spacing: 10) {
                    Button {
                        Task { await server.restart() }
                    } label: {
                        Label("Restart", systemImage: "arrow.clockwise").frame(maxWidth: .infinity)
                    }
                    .hcButton()
                    .accessibilityIdentifier("server.restart")
                    Button {
                        scene.openWeb(.local)
                    } label: {
                        Label("Web UI", systemImage: "macwindow").frame(maxWidth: .infinity)
                    }
                    .hcButton()
                }
            }
        }
        .labelStyle(.titleAndIcon)
        .lineLimit(1)
        .controlSize(.large)
    }

    @ViewBuilder private func startStopButton(wide: Bool = false) -> some View {
        switch server.phase {
        case .running:
            Button {
                Task { await server.stop() }
            } label: {
                Label("Stop", systemImage: "stop.fill")
                    .frame(maxWidth: wide ? .infinity : nil)
            }
            .accessibilityIdentifier("server.stop")
        case .starting, .stopping:
            Button {} label: {
                Label(server.phase == .starting ? "Starting" : "Stopping", systemImage: "hourglass")
                    .frame(maxWidth: wide ? .infinity : nil)
            }
            .disabled(true)
        case .stopped, .failed:
            Button {
                Task { await server.start() }
            } label: {
                Label("Start", systemImage: "play.fill")
                    .frame(maxWidth: wide ? .infinity : nil)
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
    var stacked = false

    var body: some View {
        Group {
            if stacked {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text(title).foregroundStyle(.secondary)
                    Text(value).font(.headline.monospacedDigit())
                }
            } else {
                VStack(spacing: 2) {
                    Text(value)
                        .font(.headline.monospacedDigit())
                        .contentTransition(.numericText())
                    Text(title)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity)
            }
        }
        .accessibilityElement(children: .combine)
    }
}
