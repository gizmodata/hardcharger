package com.gizmodata.hardcharger.releasetests;

import android.content.Intent;
import android.graphics.Rect;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import androidx.test.uiautomator.*;
import org.junit.Before;
import org.junit.After;
import org.junit.Test;
import org.junit.runner.RunWith;
import static org.junit.Assert.*;

/** Black-box checks: the installed production app is never loaded into the test process. */
@RunWith(AndroidJUnit4.class)
public class ReleaseWorkflowTest {
    private static final String APP = "com.gizmodata.hardcharger";
    private UiDevice device;
    @After public void saveUiEvidence() throws Exception {
        java.io.File dir = InstrumentationRegistry.getInstrumentation().getContext().getExternalFilesDir(null);
        device.takeScreenshot(new java.io.File(dir, "release-screen.png"));
        device.dumpWindowHierarchy(new java.io.File(dir, "release-window.xml"));
    }
    @Before public void launch() throws Exception {
        device = UiDevice.getInstance(InstrumentationRegistry.getInstrumentation());
        int flags = InstrumentationRegistry.getInstrumentation().getContext().getPackageManager()
            .getApplicationInfo(APP, 0).flags;
        assertEquals("Release tests require a non-debuggable app", 0,
            flags & android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE);
        device.executeShellCommand("am force-stop " + APP);
        Intent intent = InstrumentationRegistry.getInstrumentation().getContext()
            .getPackageManager().getLaunchIntentForPackage(APP);
        assertNotNull(intent);
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK);
        InstrumentationRegistry.getInstrumentation().getContext().startActivity(intent);
        await(By.desc("Server status: Running"), 120000);
    }
    private UiObject2 await(BySelector selector, long timeout) {
        UiObject2 found = device.wait(Until.findObject(selector), timeout);
        if (found == null) {
            StringBuilder visible = new StringBuilder();
            for (UiObject2 text : device.findObjects(By.pkg(APP).text(java.util.regex.Pattern.compile("(?s).+"))))
                visible.append(text.getText()).append('\n');
            fail("Missing UI: " + selector + "\nVisible app text:\n" + visible);
        }
        assertNotNull("Missing UI: " + selector, found);
        return found;
    }
    private void scroll(boolean rail, boolean down) {
        dismissTaskbarTutorial();
        android.view.accessibility.AccessibilityNodeInfo root =
            InstrumentationRegistry.getInstrumentation().getUiAutomation().getRootInActiveWindow();
        android.view.accessibility.AccessibilityNodeInfo viewport = scrollContainer(root, rail);
        assertNotNull("Missing " + (rail ? "navigation" : "content") + " scroll container", viewport);
        // Use Android's accessibility scroll action; raw swipes can be claimed by
        // the system taskbar when the emulator is resized into a short tablet.
        viewport.performAction(down ? android.view.accessibility.AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
            : android.view.accessibility.AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD);
        android.os.SystemClock.sleep(500);
        device.waitForIdle();
    }
    private android.view.accessibility.AccessibilityNodeInfo scrollContainer(
        android.view.accessibility.AccessibilityNodeInfo node, boolean rail) {
        if (node == null) return null;
        Rect bounds = new Rect(); node.getBoundsInScreen(bounds);
        if (node.isScrollable() && APP.equals(String.valueOf(node.getPackageName())) && (bounds.width() < 200) == rail)
            return node;
        for (int index = 0; index < node.getChildCount(); index++) {
            android.view.accessibility.AccessibilityNodeInfo found = scrollContainer(node.getChild(index), rail);
            if (found != null) return found;
        }
        return null;
    }
    private UiObject2 reveal(BySelector selector, boolean rail) {
        for (int direction = 0; direction < 2; direction++) {
            for (int attempt = 0; attempt < 24; attempt++) {
                dismissTaskbarTutorial();
                UiObject2 found = device.findObject(selector);
                if (found != null) {
                    Rect bounds = found.getVisibleBounds();
                    if (bounds.width() > 0 && bounds.height() >= 12) return found;
                }
                scroll(rail, direction == 0);
            }
        }
        return await(selector, 5000);
    }
    private void open(String label) {
        // Material's tablet rail exposes the visible label, while the custom phone
        // tabs expose their content description to accessibility.
        boolean tablet = device.getDisplayWidth() > device.getDisplayHeight();
        BySelector destination = switch (label) {
            case "Settings" -> By.text("Port");
            case "Console" -> By.text("SQL or .help");
            case "Web UI" -> By.text("Reconnect");
            case "Saved servers" -> By.text("Your servers");
            case "Logs" -> By.text("Follow New Lines");
            default -> By.desc("Server status: Running");
        };
        for (int attempt = 0; attempt < 3; attempt++) {
            clickControl(reveal(tablet ? By.text(label) : By.desc(label), true));
            if (device.wait(Until.hasObject(destination), 3000)) return;
        }
        await(destination, 10000);
    }
    private void dismissTaskbarTutorial() {
        String launcher = "com.google.android.apps.nexuslauncher";
        if (device.hasObject(By.pkg(launcher).text("Do more with the Taskbar"))) {
            await(By.pkg(launcher).text("Done"), 5000).click();
            assertTrue(device.wait(Until.gone(By.pkg(launcher).text("Do more with the Taskbar")), 5000));
        }
    }
    private void clickControl(UiObject2 node) {
        UiObject2 control = node;
        while (!control.isClickable() && control.getParent() != null) control = control.getParent();
        assertTrue("Control is not clickable: " + node.getText(), control.isClickable());
        control.click(100);
        device.waitForIdle();
    }
    private void about() {
        open("Settings");
        clickControl(reveal(By.text("About Hardcharger™"), false));
        await(By.text("About"), 10000);
    }
    private void sql(String command, String result) {
        await(By.desc("Clear console"), 10000).click();
        await(By.clazz("android.widget.EditText"), 10000).setText(command);
        UiObject2 keyboard = device.findObject(By.desc("Hide keyboard"));
        if (keyboard != null) keyboard.click();
        await(By.text("Run"), 10000).click();
        // Console output may contain CRLF. Read the complete accessible transcript,
        // rather than depending on UiAutomator's text-selector normalization.
        long deadline = android.os.SystemClock.uptimeMillis() + 30000;
        String visible = "";
        do {
            try {
                StringBuilder text = new StringBuilder();
                for (UiObject2 node : device.findObjects(By.pkg(APP).text(java.util.regex.Pattern.compile("(?s).+"))))
                    text.append(node.getText()).append('\n');
                visible = text.toString().replace("\r", "");
                if (visible.contains(result)) return;
            } catch (StaleObjectException replacedDuringRead) {
                // Compose replaces transcript nodes as query output arrives. Read
                // a fresh snapshot on the next bounded poll; never rerun the SQL.
            }
            android.os.SystemClock.sleep(100);
        } while (android.os.SystemClock.uptimeMillis() < deadline);
        fail("Missing SQL result: " + result + "\nActual: " + visible);
    }
    @Test public void optimizedServerQueriesAndPersistence() {
        open("Console");
        sql(".mode csv\nSELECT 4242 AS release_answer;", "release_answer\n4242");
        sql("SELECT 'café 東京 😀' AS unicode;", "unicode\ncafé 東京 😀");
        sql("SELECT json_extract('{\"answer\":73}', '$.answer') AS json_answer;", "json_answer\n73");
        sql("SELECT string_agg(extension_name, ',') FROM duckdb_extensions() WHERE loaded;", "ducklake");
        sql("SET home_directory='/data/data/com.gizmodata.hardcharger/files'; INSTALL 'http://127.0.0.1:9/forbidden.duckdb_extension';", "Installing external extensions is not supported");
        sql("LOAD '/data/local/tmp/forbidden.duckdb_extension';", "Loading external extensions is not supported");
        sql("CREATE OR REPLACE TABLE release_persistence AS SELECT 8675309 AS value; SELECT value FROM release_persistence;", "value\n8675309");
        open("Server");
        reveal(By.text("Restart"), false).click();
        await(By.text("Restart this server?"), 10000);
        // The dialog's confirmation is the last matching Restart button.
        java.util.List<UiObject2> buttons = device.findObjects(By.text("Restart"));
        buttons.get(buttons.size() - 1).click();
        await(By.desc("Server status: Running"), 120000);
        open("Console");
        await(By.desc("Clear console"), 10000).click();
        sql(".mode csv\nSELECT value FROM release_persistence;", "value\n8675309");
        about();
        reveal(By.text("Hardcharger™ server"), false);
        clickControl(reveal(By.text("Third-Party Software"), false));
        await(By.text("Hardcharger for Android"), 10000);
    }
    @Test public void optimizedTabletNavigation() {
        about();
        open("Saved servers"); await(By.text("Your servers"), 10000);
        about();
        open("Logs"); await(By.text("Follow New Lines"), 10000);
        about();
        open("Console"); await(By.text("SQL or .help"), 10000);
        about();
        open("Web UI"); await(By.text("Reconnect"), 10000);
        about();
        open("Server"); await(By.desc("Server status: Running"), 120000);
    }
}
