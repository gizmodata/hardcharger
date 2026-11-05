import CryptoKit
import UIKit
import XCTest

/// The app as a person uses it: the splash, the web UI signed in by itself running a query,
/// the SQL console, and settings that stick.
@MainActor
final class HardchargerUITests: XCTestCase {
    private var app: XCUIApplication!

    /// How long the web UI may take to open signed in. On CI's simulator it takes ~25 s (a
    /// passing 2.5.0 gate), too close to a 30 s wait: two release gates failed on it.
    private let webUILoads: TimeInterval = 90

    override func setUp() async throws {
        continueAfterFailure = false
        app = XCUIApplication()
        // A fresh app on a free port (a Mac running Hardcharger may hold 31337).
        app.launchArguments = ["-HCResetState", "YES", "-HCPort", "0", "-HCSplashMinimum", "2"]
    }

    override func tearDown() async throws {
        app.terminate()
    }

    /// Waits until the splash is gone (the app is on screen).
    /// Taps a switch and waits for its value to become `value`. A slow simulator reports the old
    /// value for a moment after the tap (2.7.0's gate read "1" right after turning one off), and
    /// can drop a tap that lands during a re-render, so it taps once more if nothing changed.
    private func flip(_ toggle: XCUIElement, to value: String, file: StaticString = #filePath, line: UInt = #line) {
        let target = NSPredicate(format: "value == %@", value)
        for _ in 0..<2 {
            toggle.switches.firstMatch.exists ? toggle.switches.firstMatch.tap() : toggle.tap()
            if XCTWaiter().wait(for: [XCTNSPredicateExpectation(predicate: target, object: toggle)], timeout: 10) == .completed {
                return
            }
        }
        XCTFail("\(toggle.identifier) is \(String(describing: toggle.value)), not \(value)", file: file, line: line)
    }

    private func waitForApp() {
        let gone = NSPredicate(format: "exists == false")
        _ = XCTWaiter().wait(for: [XCTNSPredicateExpectation(predicate: gone, object: app.descendants(matching: .any)["splash"].firstMatch)],
                             timeout: 30)
    }

    private func open(_ tab: String) {
        let button = app.tabBars.buttons[tab].exists ? app.tabBars.buttons[tab] : app.buttons[tab]
        XCTAssertTrue(button.waitForExistence(timeout: 10), "no \(tab) tab")
        button.tap()
    }

    func testSplashThenWebUIRunsSelect42() throws {
        app.launchArguments = ["-HCResetState", "YES", "-HCPort", "0", "-HCSplashMinimum", "10", "-HCInitialTab", "web"]
        app.launch()
        XCTAssertTrue(app.descendants(matching: .any)["splash"].firstMatch.waitForExistence(timeout: 5),
                      "the splash shows first")
        // (A slow simulator can take seconds per query, so the splash stays up 10 s and each
        // part is waited for: a plain `exists` once found the splash already gone.)
        XCTAssertTrue(app.images["Hardcharger™"].firstMatch.waitForExistence(timeout: 5), "with the bull and the lettering")
        XCTAssertTrue(app.descendants(matching: .any)["by GizmoData™"].firstMatch.waitForExistence(timeout: 5),
                      "and the GizmoData mark")

        let web = app.webViews.firstMatch
        XCTAssertTrue(web.waitForExistence(timeout: 30), "the web UI loads")
        // Signed in by the app: the editor's Run button, not the sign-in form.
        let run = web.buttons["Run"]
        XCTAssertTrue(run.waitForExistence(timeout: webUILoads), "the web UI opens signed in")
        XCTAssertFalse(web.secureTextFields.firstMatch.exists, "no password form")

        // Replace the editor's text with SELECT 42 and run it. (Monaco's own input is a
        // hidden textarea; a tap on its visible line focuses it.)
        let editor = web.textViews["SQL editor"]
        XCTAssertTrue(editor.waitForExistence(timeout: 10))
        // A tap below the text puts the cursor on the empty last line and opens the keyboard
        // (WebConsole.editorFocusScript); Run runs the statement at the cursor.
        let frame = editor.frame
        app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: frame.midX, dy: frame.minY + 120)).tap()
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 10), "the keyboard opens for the editor")
        // The trailing space closes the completion list ("answer" offers tpch_answers): while
        // it is open the editor's accessibility value doesn't show the line, and 2.7.0's gate
        // failed with the text on screen. The rendered word counts as proof too.
        app.typeText("SELECT 42 AS answer ")
        let typed = NSPredicate(format: "value CONTAINS 'SELECT 42 AS answer'")
        let rendered = web.staticTexts.matching(NSPredicate(format: "label == 'answer'")).firstMatch
        let landed = XCTWaiter().wait(for: [XCTNSPredicateExpectation(predicate: typed, object: editor)], timeout: 10)
            == .completed || rendered.waitForExistence(timeout: 5)
        XCTAssertTrue(landed, "the editor holds SELECT 42")
        run.tap()
        let cell = web.descendants(matching: .any).matching(NSPredicate(format: "label == '42'")).firstMatch
        XCTAssertTrue(cell.waitForExistence(timeout: 30), "the result shows 42")
        XCTAssertTrue(web.descendants(matching: .any).matching(NSPredicate(format: "label BEGINSWITH 'answer'"))
            .firstMatch.exists, "under its column, answer")
    }

    /// The native screens pass Xcode's accessibility audit: every element described for
    /// VoiceOver, with the right traits, and text that isn't clipped. (The web UI has its own
    /// checks in ui/.)
    func testAccessibilityAudit() throws {
        app.launchArguments += ["-HCInitialTab", "server"]
        app.launch()
        waitForApp()
        XCTAssertTrue(app.buttons["server.stop"].firstMatch.waitForExistence(timeout: 30))
        for tab in ["Server", "Settings", "Console"] {
            open(tab)
            Thread.sleep(forTimeInterval: 1)
            // (Dynamic Type: every font is a text style; the audit's own check flags List rows
            // with long monospaced values alternately, so it is checked by eye at the largest
            // accessibility size instead, ios/README.md.)
            try auditScreen(tab)
        }
    }

    /// One accessibility audit of the screen shown. An audit that couldn't finish in time
    /// (a slow simulator: Code=-56) runs again, twice at most; any issue it finds fails the test.
    private func auditScreen(_ tab: String) throws {
        for attempt in 1...3 {
            do {
                try app.performAccessibilityAudit(for: [.sufficientElementDescription, .trait, .textClipped]) { issue in
                    // System controls the app doesn't draw (the tab bar's own items, keyboard).
                    let element = issue.element
                    print("AUDIT>>> \(tab): \(issue.auditType) \(issue.compactDescription) " +
                          "[\(element?.elementType.rawValue ?? 0) \(element?.identifier ?? "") \(element?.label ?? "")]")
                    return false
                }
                return
            } catch let error as NSError
                where error.domain == "com.apple.xcode.xctest.accessibilityAudit" && error.code == -56 && attempt < 3
            {
                print("AUDIT>>> \(tab): the audit didn't finish in time (attempt \(attempt)); again")
            }
        }
    }

    func testWebUIKeyboardCanBeHidden() throws {
        app.launchArguments += ["-HCInitialTab", "web"]
        app.launch()
        waitForApp()
        let web = app.webViews.firstMatch
        let editor = web.textViews["SQL editor"]
        XCTAssertTrue(editor.waitForExistence(timeout: webUILoads))
        // After Run, WebKit brings the keyboard up for the page's editor.
        web.buttons["Run"].firstMatch.tap()
        XCTAssertTrue(web.descendants(matching: .any).matching(NSPredicate(format: "label BEGINSWITH 'version'")).firstMatch
            .waitForExistence(timeout: 30))
        if !app.keyboards.firstMatch.waitForExistence(timeout: 5) {
            let frame = editor.frame
            app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: frame.midX, dy: frame.minY + 120)).tap()
        }
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 10), "a keyboard is up")
        let hide = app.buttons["webui.hideKeyboard"].firstMatch
        XCTAssertTrue(hide.waitForExistence(timeout: 5), "the toolbar offers to hide it")
        hide.tap()
        let gone = NSPredicate(format: "exists == false")
        XCTAssertEqual(XCTWaiter().wait(for: [XCTNSPredicateExpectation(predicate: gone, object: app.keyboards.firstMatch)],
                                        timeout: 10), .completed, "and it goes")
    }

    func testConsoleRunsSelect42AndTables() throws {
        app.launchArguments += ["-HCInitialTab", "console"]
        app.launch()
        waitForApp()
        let input = app.textViews["sqlconsole.input"]
        XCTAssertTrue(input.waitForExistence(timeout: 30))
        XCTAssertTrue(app.staticTexts.containing(NSPredicate(format: "label BEGINSWITH 'Statements end with'"))
            .firstMatch.waitForExistence(timeout: 30), "connected")
        input.tap()

        // Return on an open statement adds a line; the ';' runs it.
        input.typeText("SELECT 42\n")
        input.typeText("AS answer;\n")
        let answer = app.consoleOutput(containing: "│      42 │")
        XCTAssertTrue(answer.waitForExistence(timeout: 30), "the table shows 42")
        XCTAssertTrue(app.consoleOutput(containing: "│ answer  │").exists)
        XCTAssertEqual((input.value as? String) ?? "", "", "the input is cleared after it runs")

        input.typeText("CREATE TABLE people AS SELECT 1 AS id, 'Ada' AS name;\n")
        XCTAssertTrue(app.consoleOutput(containing: "OK, 1 row affected").waitForExistence(timeout: 30))
        input.typeText(".tables\n")
        XCTAssertTrue(app.consoleOutput(containing: "people")
            .waitForExistence(timeout: 30), ".tables lists people")
    }

    func testSettingsTogglesStick() throws {
        app.launchArguments += ["-HCInitialTab", "settings"]
        app.launch()
        waitForApp()
        let printQueries = app.switches["settings.printQueries"]
        XCTAssertTrue(printQueries.waitForExistence(timeout: 30))
        XCTAssertEqual(printQueries.value as? String, "1")
        flip(printQueries, to: "0")
        // A setting the running server started with: Settings offers the restart.
        XCTAssertTrue(app.buttons["settings.restart"].waitForExistence(timeout: 10))

        let autoStart = app.switches["settings.startAutomatically"]
        flip(autoStart, to: "0")

        // Again, without the reset: both stuck.
        app.terminate()
        app.launchArguments = ["-HCPort", "0", "-HCSplashMinimum", "0.2", "-HCInitialTab", "settings"]
        app.launch()
        XCTAssertTrue(app.switches["settings.printQueries"].waitForExistence(timeout: 30))
        XCTAssertEqual(app.switches["settings.printQueries"].value as? String, "0")
        XCTAssertEqual(app.switches["settings.startAutomatically"].value as? String, "0")
    }

    func testShareOnLocalNetworkServesTLSOnTheLAN() throws {
        app.launchArguments += ["-HCInitialTab", "server", "-HCInitialSheet", "share"]
        app.launch()
        waitForApp()
        let toggle = app.switches["share.toggle"]
        XCTAssertTrue(toggle.waitForExistence(timeout: 30))
        toggle.switches.firstMatch.exists ? toggle.switches.firstMatch.tap() : toggle.tap()
        // A warning first, then the server restarts on every interface with TLS.
        app.buttons["Share"].firstMatch.tap()
        let address = app.staticTexts["share.url"].firstMatch
        XCTAssertTrue(address.waitForExistence(timeout: 30), "an address on the network")
        let url = try XCTUnwrap(URL(string: address.label))
        XCTAssertEqual(url.scheme, "https")
        XCTAssertNotEqual(url.host(), "127.0.0.1")
        let fingerprintText = app.staticTexts["share.fingerprint"].firstMatch
        for _ in 0..<6 where !fingerprintText.exists { app.swipeUp() }
        XCTAssertTrue(fingerprintText.waitForExistence(timeout: 5), "the certificate's fingerprint")
        let fingerprint = fingerprintText.label
        XCTAssertEqual(fingerprint.count, 95, "a SHA-256 fingerprint")
        XCTAssertTrue(app.images.containing(NSPredicate(format: "label BEGINSWITH 'QR code for'")).firstMatch.exists)

        // Another device on the network: TLS with exactly that certificate.
        // The app shows the address as the server restarts on every interface, so the first
        // request can land before the new listener is up (2.7.0's gate: no answer, then
        // "connection refused"). Ask again, each request bounded, until it answers or 90 s pass.
        let pin = Pin(fingerprint: fingerprint)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 10
        let session = URLSession(configuration: configuration, delegate: pin, delegateQueue: nil)
        let health = url.deletingLastPathComponent().appending(path: "chargewire/v1/health")
        let status = Status()
        let deadline = Date().addingTimeInterval(90)
        while status.code != 200 && Date() < deadline {
            // A semaphore, not an XCTest expectation: an attempt that doesn't answer in time (a
            // TLS handshake can stall past the request timeout) is cancelled and tried again,
            // where an unfulfilled expectation failed the whole test on the first slow try.
            let answered = DispatchSemaphore(value: 0)
            let task = session.dataTask(with: health) { _, response, _ in
                status.code = (response as? HTTPURLResponse)?.statusCode ?? 0
                answered.signal()
            }
            task.resume()
            if answered.wait(timeout: .now() + 15) == .timedOut {
                task.cancel()
                _ = answered.wait(timeout: .now() + 5)  // the cancelled task's own answer
            }
            if status.code != 200 { Thread.sleep(forTimeInterval: 2) }
        }
        XCTAssertEqual(status.code, 200, "the server answers on its LAN address")
        XCTAssertTrue(pin.matched, "with the certificate the app shows")

        // This device's own web UI and console, now over TLS with the same certificate.
        app.buttons["Done"].firstMatch.tap()
        open("Web UI")
        XCTAssertTrue(app.webViews.firstMatch.buttons["Run"].waitForExistence(timeout: webUILoads), "the web UI over TLS")
        open("Console")
        let input = app.textViews["sqlconsole.input"]
        XCTAssertTrue(app.staticTexts.containing(NSPredicate(format: "label BEGINSWITH 'Statements end with'"))
            .firstMatch.waitForExistence(timeout: 30), "the console connects over TLS")
        input.tap()
        input.typeText("SELECT 42 AS answer;\n")
        XCTAssertTrue(app.consoleOutput(containing: "│      42 │")
            .waitForExistence(timeout: 30))
    }

    func testServerStopsAndStartsAgain() throws {
        app.launchArguments += ["-HCInitialTab", "server"]
        app.launch()
        waitForApp()
        let stop = app.buttons["server.stop"].firstMatch
        XCTAssertTrue(stop.waitForExistence(timeout: 30), "running")
        stop.tap()
        let start = app.buttons["server.start"].firstMatch
        XCTAssertTrue(start.waitForExistence(timeout: 30), "stopped")
        start.tap()
        XCTAssertTrue(app.buttons["server.stop"].firstMatch.waitForExistence(timeout: 30), "running again")
    }
}

private final class Status: @unchecked Sendable {
    var code = 0
}

/// Trusts exactly the certificate with this fingerprint (as a person comparing it would).
private final class Pin: NSObject, URLSessionDelegate, @unchecked Sendable {
    let fingerprint: String
    var matched = false
    init(fingerprint: String) { self.fingerprint = fingerprint }

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge) async
        -> (URLSession.AuthChallengeDisposition, URLCredential?)
    {
        guard let trust = challenge.protectionSpace.serverTrust,
              let leaf = (SecTrustCopyCertificateChain(trust) as? [SecCertificate])?.first
        else { return (.cancelAuthenticationChallenge, nil) }
        let presented = SHA256.hash(data: SecCertificateCopyData(leaf) as Data)
            .map { String(format: "%02X", $0) }.joined(separator: ":")
        matched = presented == fingerprint
        return matched ? (.useCredential, URLCredential(trust: trust)) : (.cancelAuthenticationChallenge, nil)
    }
}

extension XCUIApplication {
    /// A console output (a text view) whose text contains `text`.
    func consoleOutput(containing text: String) -> XCUIElement {
        textViews.matching(NSPredicate(format: "identifier == 'sqlconsole.output' AND value CONTAINS %@", text)).firstMatch
    }
}
