import XCTest

/// The app against a Hardcharger server elsewhere (ios/scripts/test-remote.sh starts it on
/// the Mac's loopback, with a mock identity provider): a saved server's web UI and console
/// with a password, then browser sign-in. Fails, never skips, without that server.
@MainActor
final class RemoteServerUITests: XCTestCase {
    private var app: XCUIApplication!
    private var port = ""
    private var password = ""

    override func setUp() async throws {
        continueAfterFailure = false
        let env = ProcessInfo.processInfo.environment
        port = try XCTUnwrap(env["HC_REMOTE_PORT"], "run through ios/scripts/test-remote.sh")
        password = try XCTUnwrap(env["HC_REMOTE_PASSWORD"])
        app = XCUIApplication()
        app.launchArguments = ["-HCResetState", "YES", "-HCPort", "0", "-HCSplashMinimum", "0.3",
                               "-HCInitialTab", "settings"]
        app.launch()
        let gone = NSPredicate(format: "exists == false")
        await fulfillment(of: [XCTNSPredicateExpectation(
            predicate: gone, object: app.descendants(matching: .any)["splash"].firstMatch)], timeout: 30)
    }

    override func tearDown() async throws {
        app.terminate()
    }

    /// Settings › Saved Servers › + : address, name, sign-in, Test, Save.
    private func addServer(name: String, browser: Bool) {
        let saved = app.buttons["settings.servers"]
        if saved.waitForExistence(timeout: 5) { saved.tap() } else { app.swipeUp(); app.buttons["settings.servers"].tap() }
        app.buttons["servers.add"].tap()
        let address = app.textFields["editor.address"]
        XCTAssertTrue(address.waitForExistence(timeout: 10))
        address.tap()
        address.typeText("127.0.0.1:\(port)")
        app.textFields["editor.name"].tap()
        app.textFields["editor.name"].typeText(name)
        if browser {
            app.buttons.containing(NSPredicate(format: "label BEGINSWITH 'Sign in with'")).firstMatch.tap()
            app.buttons["Single sign-on (browser)"].firstMatch.tap()
        } else {
            app.textFields["editor.username"].tap()
            app.textFields["editor.username"].typeText("hardcharger")
            app.secureTextFields["editor.password"].tap()
            app.secureTextFields["editor.password"].typeText(password)
        }
        app.buttons["editor.test"].tap()
        let found = app.staticTexts.containing(NSPredicate(format: "label BEGINSWITH 'Hardcharger 2'")).firstMatch
        XCTAssertTrue(found.waitForExistence(timeout: 20), "Test reaches the server")
        app.buttons["editor.save"].tap()
        let closed = NSPredicate(format: "exists == false")
        XCTAssertEqual(XCTWaiter().wait(for: [XCTNSPredicateExpectation(predicate: closed,
                                                                        object: app.buttons["editor.save"])],
                                        timeout: 10), .completed, "the editor closes")
        // iOS may offer to save the password it saw in a secure field.
        for owner in [app!, XCUIApplication(bundleIdentifier: "com.apple.springboard")] {
            let notNow = owner.buttons["Not Now"]
            if notNow.waitForExistence(timeout: 2) { notNow.tap() }
        }
        XCTAssertTrue(app.buttons.containing(NSPredicate(format: "label CONTAINS %@", name)).firstMatch
            .waitForExistence(timeout: 10), "saved")
    }

    func testPasswordServerWebUIAndConsole() throws {
        addServer(name: "Mac Remote", browser: false)
        app.buttons.containing(NSPredicate(format: "label CONTAINS 'Mac Remote'")).firstMatch.tap()
        // Its web UI, signed in by the app.
        let web = app.webViews.firstMatch
        XCTAssertTrue(web.buttons["Run"].waitForExistence(timeout: 30), "the remote web UI opens signed in")
        XCTAssertTrue(web.buttons["remote"].waitForExistence(timeout: 10), "showing the remote database's catalog")

        // The console on the same server.
        app.tabBars.buttons["Console"].tap()
        app.buttons["sqlconsole.target"].tap()
        app.buttons["Mac Remote"].firstMatch.tap()
        XCTAssertTrue(app.staticTexts.containing(NSPredicate(format: "label BEGINSWITH 'Statements end with'"))
            .firstMatch.waitForExistence(timeout: 30), "the console connects")
        let input = app.textViews["sqlconsole.input"]
        input.tap()
        input.typeText("SELECT name FROM remote_people;\n")
        XCTAssertTrue(app.staticTexts.containing(NSPredicate(format: "label CONTAINS 'Grace'")).firstMatch
            .waitForExistence(timeout: 30), "the remote table answers")
    }

    func testBrowserSignIn() throws {
        addServer(name: "Mac SSO", browser: true)
        app.buttons.containing(NSPredicate(format: "label CONTAINS 'Mac SSO'")).firstMatch.tap()
        // iOS asks before an app opens a sign-in page.
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        let proceed = springboard.buttons["Continue"]
        XCTAssertTrue(proceed.waitForExistence(timeout: 20), "the sign-in sheet asks")
        proceed.tap()
        // The mock provider approves at once; the app collects the token and closes the sheet.
        XCTAssertTrue(app.webViews.firstMatch.buttons["Run"].waitForExistence(timeout: 60),
                      "the web UI opens signed in through the identity provider")
    }
}
