package com.gizmodata.hardcharger

import android.graphics.Bitmap
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.test.*
import androidx.compose.ui.text.TextLayoutResult
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.test.espresso.Espresso
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * #57: a boxed result table and `.tables` in the console, captured for review and for the store
 * listing (`adb pull /sdcard/Android/data/com.gizmodata.hardcharger/files/screenshots/`).
 * Run it with the emulator in dark mode (`adb shell cmd uimode night yes`) for the dark capture.
 */
@RunWith(AndroidJUnit4::class)
class ConsoleScreenshotTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()

    private fun openConsole(): AppModel {
        val model = (compose.activity.application as HardchargerApplication).model
        compose.waitUntil(120_000) { model.running && !model.busy }
        compose.onNodeWithContentDescription("Console", useUnmergedTree = true).performClick()
        compose.onNodeWithContentDescription("Clear console").performClick()
        return model
    }

    private fun run(model: AppModel, sql: String, expect: String) {
        compose.onNodeWithText("SQL or .help").performTextInput(sql)
        compose.onNodeWithText("Run", useUnmergedTree = true).performClick()
        compose.waitUntil(30_000) {
            !model.querying && runCatching { compose.onNodeWithText(expect, substring = true).assertExists() }.isSuccess
        }
    }

    private fun screenshot(name: String) {
        val shot: Bitmap = InstrumentationRegistry.getInstrumentation().uiAutomation.takeScreenshot()
        val directory = File(compose.activity.getExternalFilesDir(null), "screenshots").apply { mkdirs() }
        File(directory, "$name.png").outputStream().use { shot.compress(Bitmap.CompressFormat.PNG, 100, it) }
    }

    /**
     * #57, checked on Android like iOS: a result many screens wide, then `.tables`, keep every
     * line whole (the transcript scrolls sideways) at the device's font scale. Run it at 1.0 and
     * at the largest (`adb shell settings put system font_scale 2.0`); the capture is named by it.
     */
    @Test fun wideResultsKeepTheirLines() {
        val model = openConsole()
        run(model, "CREATE OR REPLACE TABLE console_orders AS SELECT range AS n, chr(97 + range::INTEGER) || (range * 7) AS label, " +
            "(range * 2.5)::DECIMAL(9,2) AS amount FROM range(4);", "console_orders")
        val started = System.nanoTime()
        run(model, "SELECT *, 'the wide one' AS marker FROM console_orders a, console_orders b, console_orders c, " +
            "console_orders d ORDER BY ALL LIMIT 3;", "the wide one")
        run(model, ".tables", "console_orders")
        compose.waitForIdle()
        assertTrue("laying the wide result out took too long", System.nanoTime() - started < 30_000_000_000L)
        Espresso.closeSoftKeyboard()
        compose.waitForIdle()
        // The transcript is one Text: unwrapped, it lays out a line per line of its text.
        val node = compose.onNode(hasText("the wide one", substring = true)).fetchSemanticsNode()
        val layouts = mutableListOf<TextLayoutResult>()
        node.config[SemanticsActions.GetTextLayoutResult].action!!.invoke(layouts)
        val layout = layouts.first()
        val text = layout.layoutInput.text.text
        assertEquals("no line wrapped", text.count { it == '\n' } + 1, layout.lineCount)
        val screen = compose.activity.resources.displayMetrics.widthPixels
        assertTrue("the result is wider than the screen", layout.size.width > screen)
        val scale = compose.activity.resources.configuration.fontScale
        screenshot("console-wide-${"%.1f".format(java.util.Locale.ROOT, scale)}")
    }

    @Test fun boxedTablesInTheConsole() {
        val model = openConsole()
        fun run(sql: String, expect: String) = run(model, sql, expect)
        run("CREATE OR REPLACE TABLE console_orders AS SELECT range AS n, chr(97 + range::INTEGER) || (range * 7) AS label, " +
            "(range * 2.5)::DECIMAL(9,2) AS amount FROM range(4);", "console_orders")
        run("SELECT * FROM console_orders;", "decimal(9,2)")
        Espresso.closeSoftKeyboard()
        compose.waitForIdle()
        val shot: Bitmap = InstrumentationRegistry.getInstrumentation().uiAutomation.takeScreenshot()
        val night = compose.activity.resources.configuration.uiMode and android.content.res.Configuration.UI_MODE_NIGHT_MASK ==
            android.content.res.Configuration.UI_MODE_NIGHT_YES
        val directory = File(compose.activity.getExternalFilesDir(null), "screenshots").apply { mkdirs() }
        File(directory, "console-table-${if (night) "dark" else "light"}.png").outputStream().use {
            shot.compress(Bitmap.CompressFormat.PNG, 100, it)
        }
    }
}
