import SwiftUI
import UniformTypeIdentifiers

/// A window: the splash while the server starts, then the app. A sidebar on iPad (and in
/// wide windows), a tab bar on iPhone and in narrow Stage Manager or Split View windows.
struct RootView: View {
    @Environment(AppModel.self) private var model
    @Environment(ServerController.self) private var server
    @State private var scene = SceneModel()
    @State private var splashDone = false
    @State private var dropTargeted = false
    @Environment(\.horizontalSizeClass) private var sizeClass

    var body: some View {
        ZStack {
            if splashDone {
                main
                    .transition(.opacity)
            } else {
                SplashView()
                    // Nothing to tap, and while it fades out, taps belong to the app.
                    .allowsHitTesting(false)
                    .transition(.opacity.combined(with: .scale(scale: 1.04)))
            }
        }
        .animation(.easeInOut(duration: 0.45), value: splashDone)
        .task { await finishSplash() }
        .environment(scene)
    }

    private var main: some View {
        @Bindable var scene = scene
        // The tab bar (iPhone, narrow windows) has the four everyday screens; the sidebar
        // (iPad) also lists the log and the saved servers, which iPhone reaches from Server
        // and Settings.
        return TabView(selection: $scene.tab) {
            Tab("Web UI", systemImage: "macwindow", value: AppTab.web) {
                WebUIScreen()
            }
            .accessibilityIdentifier("tab.web")
            Tab("Console", systemImage: "terminal", value: AppTab.console) {
                SQLConsoleScreen()
            }
            .accessibilityIdentifier("tab.console")
            Tab("Server", systemImage: "server.rack", value: AppTab.server) {
                ServerScreen()
            }
            .accessibilityIdentifier("tab.server")
            Tab("Settings", systemImage: "gearshape", value: AppTab.settings) {
                SettingsScreen()
            }
            .accessibilityIdentifier("tab.settings")
            if sizeClass == .regular {
                TabSection("More") {
                    Tab("Logs", systemImage: "text.line.first.and.arrowtriangle.forward", value: AppTab.logs) {
                        LogsScreen()
                    }
                    .accessibilityIdentifier("tab.logs")
                    Tab("Servers", systemImage: "network", value: AppTab.servers) {
                        RemoteServersScreen()
                    }
                    .accessibilityIdentifier("tab.servers")
                }
            }
        }
        .onChange(of: sizeClass) { _, size in
            // Narrower (Split View, Stage Manager): the tab bar has no Logs or Servers.
            if size != .regular, scene.tab == .logs { scene.tab = .server }
            if size != .regular, scene.tab == .servers { scene.tab = .settings }
        }
        .tabViewStyle(.sidebarAdaptable)
        .background { TabShortcuts(scene: scene) }
        .fileImporter(isPresented: $scene.showImporter, allowedContentTypes: DataImport.contentTypes,
                      allowsMultipleSelection: true) { result in
            if case .success(let urls) = result {
                Task { await scene.importFiles(urls, server: server) }
            }
        }
        .sheet(isPresented: $scene.showShare) {
            NavigationStack { ShareScreen() }
        }
        .sheet(item: Binding(get: { scene.importResults.isEmpty ? nil : ImportSummary(results: scene.importResults) },
                             set: { if $0 == nil { scene.importResults = [] } })) { summary in
            ImportResultSheet(results: summary.results)
        }
        .alert("Import failed", isPresented: Binding(get: { scene.importError != nil },
                                                      set: { if !$0 { scene.importError = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(scene.importError ?? "")
        }
        .overlay {
            if scene.isImporting {
                ProgressView("Importing…")
                    .padding(24)
                    .hcGlass(cornerRadius: 20)
            }
        }
        .overlay {
            if dropTargeted {
                RoundedRectangle(cornerRadius: 24)
                    .strokeBorder(Color.accentColor, style: StrokeStyle(lineWidth: 4, dash: [12, 8]))
                    .padding(12)
                    .overlay {
                        Label("Drop to import into Hardcharger", systemImage: "square.and.arrow.down")
                            .font(.title3.weight(.semibold))
                            .padding()
                            .hcGlass(cornerRadius: 16)
                    }
                    .allowsHitTesting(false)
            }
        }
        // iPad: drop CSV, JSON or Parquet files from Files or another app onto the window.
        .dropDestination(for: URL.self) { urls, _ in
            let files = urls.filter(\.isFileURL)
            guard !files.isEmpty else { return false }
            Task { await scene.importFiles(files, server: server) }
            return true
        } isTargeted: { dropTargeted = $0 }
        .onChange(of: model.pendingImports, initial: true) { _, pending in
            guard !pending.isEmpty else { return }
            let files = pending
            model.pendingImports = []
            Task { await scene.importFiles(files, server: server) }
        }
        .onChange(of: model.pendingConnection) { _, connection in
            if connection != nil { scene.tab = sizeClass == .regular ? .servers : .settings }
        }
    }

    private func finishSplash() async {
        if let tab = model.launch.initialTab { scene.tab = tab }
        if model.launch.initialSheet == "share" { scene.showShare = true }
        if model.launch.splashOnly { return }
        let started = ContinuousClock.now
        try? await Task.sleep(for: .seconds(model.launch.splashMinimum))
        // Until the server is up (or 4 seconds, whichever is first).
        while server.phase == .starting, ContinuousClock.now - started < .seconds(4) {
            try? await Task.sleep(for: .milliseconds(100))
        }
        splashDone = true
    }
}

private struct ImportSummary: Identifiable {
    let results: [DataImport.Result]
    var id: [UUID] { results.map(\.id) }
}

/// ⌘1…⌘6 switch tabs; ⌘I imports; ⌘⇧L opens Share on Local Network.
private struct TabShortcuts: View {
    @Bindable var scene: SceneModel

    var body: some View {
        ZStack {
            ForEach(Array(AppTab.allCases.enumerated()), id: \.element) { index, tab in
                Button("") { scene.tab = tab }
                    .keyboardShortcut(KeyEquivalent(Character("\(index + 1)")), modifiers: .command)
            }
            Button("") { scene.showImporter = true }
                .keyboardShortcut("i", modifiers: .command)
            Button("") { scene.showShare = true }
                .keyboardShortcut("l", modifiers: [.command, .shift])
        }
        .opacity(0)
        .accessibilityHidden(true)
    }
}
