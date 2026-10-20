import XCTest

/// The README's and App Store's screenshots (ios/scripts/screenshots.sh): the app with TPC-H
/// sample data, every main screen, written to HC_SCREENSHOT_DIR as <HC_SCREENSHOT_PREFIX>-<screen>.png.
/// Not a test of behaviour (the other suites are); not in the CI gate.
@MainActor
final class ScreenshotTests: XCTestCase {
    private var app: XCUIApplication!
    private var directory: URL!
    private var prefix = ""

    override func setUp() async throws {
        continueAfterFailure = false
        let env = ProcessInfo.processInfo.environment
        directory = URL(filePath: try XCTUnwrap(env["HC_SCREENSHOT_DIR"], "run through ios/scripts/screenshots.sh"))
        prefix = env["HC_SCREENSHOT_PREFIX"] ?? "shot"
        app = XCUIApplication()
    }

    private func shot(_ name: String) throws {
        Thread.sleep(forTimeInterval: 1.0)
        let png = XCUIScreen.main.screenshot().pngRepresentation
        try png.write(to: directory.appending(path: "\(prefix)-\(name).png"))
    }

    private func waitForApp() {
        let gone = NSPredicate(format: "exists == false")
        _ = XCTWaiter().wait(for: [XCTNSPredicateExpectation(
            predicate: gone, object: app.descendants(matching: .any)["splash"].firstMatch)], timeout: 30)
    }

    private func open(_ tab: String) {
        let inBar = app.tabBars.buttons[tab]
        if inBar.exists { inBar.tap() } else { app.buttons[tab].firstMatch.tap() }
    }

    private var isPad: Bool { UIDevice.current.userInterfaceIdiom == .pad }

    func testScreens() throws {
        // The splash, as it shows while the server starts.
        app.launchArguments = ["-HCResetState", "YES", "-HCSplashOnly", "YES"]
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["splash"].firstMatch.waitForExistence(timeout: 10))
        Thread.sleep(forTimeInterval: 2.5)
        try shot("splash")
        app.terminate()

        // Sample data through the console, then the console itself.
        app.launchArguments = ["-HCSplashMinimum", "0.3", "-HCInitialTab", "console"]
        app.launch()
        waitForApp()
        let input = app.textViews["sqlconsole.input"]
        XCTAssertTrue(app.staticTexts.containing(NSPredicate(format: "label BEGINSWITH 'Statements end with'"))
            .firstMatch.waitForExistence(timeout: 30))
        input.tap()
        input.typeText("CALL dbgen(sf => 0.01);\n")
        XCTAssertTrue(app.consoleOutput(containing: "OK")
            .waitForExistence(timeout: 60))
        input.typeText(".tables\n")
        input.typeText("SELECT l_returnflag AS flag, l_linestatus AS status,\n")
        input.typeText("  count(*) AS items, round(sum(l_extendedprice), 2) AS revenue\n")
        input.typeText("FROM lineitem GROUP BY ALL ORDER BY ALL;\n")
        XCTAssertTrue(app.consoleOutput(containing: "revenue")
            .waitForExistence(timeout: 30))
        app.buttons["sqlconsole.hideKeyboard"].tap()
        try shot("console")

        // The web UI with a query and its results.
        open("Web UI")
        let web = app.webViews.firstMatch
        let editor = web.textViews["SQL editor"]
        XCTAssertTrue(editor.waitForExistence(timeout: 30))
        // The web UI as it opens (on a phone, Run would bring up the keyboard).
        Thread.sleep(forTimeInterval: 1.5)
        try shot("webui")

        open("Server")
        XCTAssertTrue(app.buttons["server.stop"].firstMatch.waitForExistence(timeout: 30))
        try shot("server")

        if isPad {
            open("Logs")
        } else {
            let logs = app.buttons["server.logs"].firstMatch
            for _ in 0..<6 where !logs.isHittable { app.swipeUp() }
            logs.tap()
        }
        Thread.sleep(forTimeInterval: 1.5)
        try shot("logs")

        open("Settings")
        XCTAssertTrue(app.switches["settings.printQueries"].waitForExistence(timeout: 10))
        try shot("settings")

        // Sharing, on.
        app.buttons["settings.share"].tap()
        let toggle = app.switches["share.toggle"]
        XCTAssertTrue(toggle.waitForExistence(timeout: 10))
        toggle.switches.firstMatch.exists ? toggle.switches.firstMatch.tap() : toggle.tap()
        app.buttons["Share"].firstMatch.tap()
        XCTAssertTrue(app.staticTexts["share.url"].firstMatch.waitForExistence(timeout: 30))
        try shot("share")
        app.terminate()
    }
}

extension XCUIApplication {
    /// A console output (a text view) whose text contains `text`.
    func consoleOutput(containing text: String) -> XCUIElement {
        textViews.matching(NSPredicate(format: "identifier == 'sqlconsole.output' AND value CONTAINS %@", text)).firstMatch
    }
}
