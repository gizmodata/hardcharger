import SwiftUI

@main
struct HardchargerApp: App {
    @State private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase
    @UIApplicationDelegateAdaptor private var delegate: AppDelegate

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
                .environment(model.server)
                .environment(model.remotes)
                .onOpenURL { model.handle(url: $0) }
                .task { await model.appLaunched() }
        }
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .active:
                Task { await model.server.resumeAfterBackground() }
            case .background:
                delegate.keepServingBriefly()
            default:
                break
            }
        }
        .commands {
            ServerCommands(model: model)
        }
    }
}

/// The app's server, for process-level hooks that have no SwiftUI environment.
@MainActor
enum AppLifecycle {
    static weak var server: ServerController?
}

/// Process-level hooks SwiftUI doesn't offer.
final class AppDelegate: NSObject, UIApplicationDelegate {
    private var backgroundTask: UIBackgroundTaskIdentifier = .invalid

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool
    {
        // A client that drops a connection mid-write must not end the app with SIGPIPE
        // (Boost.Asio sets SO_NOSIGPIPE on its sockets; this covers every other write).
        signal(SIGPIPE, SIG_IGN)
        // OpenSSL's CA certificates (iOS has no bundle on disk): before the server or a
        // console makes an outbound TLS connection.
        CABundle.install()
        return true
    }

    /// iOS suspends an app soon after it leaves the screen; ask for the usual grace time so
    /// statements running then can finish.
    @MainActor
    func keepServingBriefly() {
        guard backgroundTask == .invalid else { return }
        backgroundTask = UIApplication.shared.beginBackgroundTask(withName: "Hardcharger statements") { [weak self] in
            self?.endBackgroundTask()
        }
    }

    @MainActor
    private func endBackgroundTask() {
        guard backgroundTask != .invalid else { return }
        UIApplication.shared.endBackgroundTask(backgroundTask)
        backgroundTask = .invalid
    }

    /// The app is ending while it runs (rare on iOS, which suspends apps instead): stop the
    /// server at once, so DuckDB closes the database cleanly.
    func applicationWillTerminate(_ application: UIApplication) {
        MainActor.assumeIsolated {
            AppLifecycle.server?.stopForTermination()
        }
    }
}

/// Menu-bar commands (iPad with a keyboard): start, stop and restart the server, import.
struct ServerCommands: Commands {
    let model: AppModel

    var body: some Commands {
        CommandMenu("Server") {
            Button("Start Server") { Task { await model.server.start() } }
                .keyboardShortcut("r", modifiers: [.command, .shift])
                .disabled(model.server.phase == .running || model.server.phase.isBusy)
            Button("Stop Server") { Task { await model.server.stop() } }
                .keyboardShortcut(".", modifiers: [.command])
                .disabled(model.server.phase != .running)
            Button("Restart Server") { Task { await model.server.restart() } }
                .keyboardShortcut("r", modifiers: [.command, .option])
                .disabled(model.server.phase != .running)
        }
    }
}
