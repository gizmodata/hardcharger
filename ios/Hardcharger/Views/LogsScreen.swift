import SwiftUI

/// The Logs tab.
struct LogsScreen: View {
    var body: some View {
        NavigationStack { LogsView() }
    }
}

/// The server's log, live: with level marks (as Hardcharger's log_emoji draws them) or
/// plain, filtered by level and text.
struct LogsView: View {
    @Environment(ServerController.self) private var server
    @State private var tail: LogTail?
    @State private var minimumLevel: LogLine.Level = .debug
    @State private var search = ""
    @State private var follow = true

    var body: some View {
        @Bindable var server = server
            Group {
                if let tail {
                    LogList(lines: filtered(tail.lines), style: server.settings.logStyle, follow: follow)
                        .overlay {
                            if tail.lines.isEmpty {
                                ContentUnavailableView("No log yet", systemImage: "text.alignleft",
                                                       description: Text("The server writes its log here when it runs."))
                            }
                        }
                } else {
                    ProgressView()
                }
            }
            .navigationTitle("Logs")
            .navigationBarTitleDisplayMode(.inline)
            .searchable(text: $search, prompt: Text("Filter"))
            .toolbar {
                ToolbarItem(placement: .principal) {
                    Picker("Style", selection: $server.settings.logStyle) {
                        ForEach(LogStyle.allCases) { style in
                            Text(style.label).tag(style)
                        }
                    }
                    .pickerStyle(.segmented)
                    .frame(maxWidth: 200)
                    .accessibilityIdentifier("logs.style")
                }
                ToolbarItemGroup(placement: .primaryAction) {
                    Menu {
                        Picker("Show", selection: $minimumLevel) {
                            Text("Everything").tag(LogLine.Level.debug)
                            Text("Info and above").tag(LogLine.Level.info)
                            Text("Warnings and errors").tag(LogLine.Level.warning)
                            Text("Errors").tag(LogLine.Level.error)
                        }
                        Toggle("Follow New Lines", isOn: $follow)
                        Divider()
                        ShareLink(item: server.paths.logFile) {
                            Label("Share Log File", systemImage: "square.and.arrow.up")
                        }
                        Button {
                            tail?.clear()
                        } label: {
                            Label("Clear View", systemImage: "eraser")
                        }
                    } label: {
                        Label("Options", systemImage: "line.3.horizontal.decrease.circle")
                    }
                }
            }
        .onAppear {
            if tail == nil { tail = LogTail(file: server.paths.logFile) }
            tail?.start()
        }
        .onDisappear { tail?.stop() }
    }

    private func filtered(_ lines: [LogLine]) -> [LogLine] {
        lines.filter { line in
            (line.level.map { $0 >= minimumLevel } ?? true)
                && (search.isEmpty || line.raw.localizedCaseInsensitiveContains(search))
        }
    }
}

private struct LogList: View {
    let lines: [LogLine]
    let style: LogStyle
    let follow: Bool
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView([.vertical]) {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(lines) { line in
                        Text(line.text(style: style))
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(color(line))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .textSelection(.enabled)
                            .id(line.id)
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
            }
            .defaultScrollAnchor(.bottom)
            .onChange(of: lines.last?.id) { _, last in
                guard follow, let last else { return }
                proxy.scrollTo(last, anchor: .bottom)
            }
            .accessibilityIdentifier("logs.list")
        }
    }

    private func color(_ line: LogLine) -> Color {
        switch line.level {
        case .error: .red
        case .warning: .orange
        case .debug: .secondary
        default: line.outcome == .success ? .green : .primary
        }
    }
}
