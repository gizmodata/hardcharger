package com.gizmodata.hardcharger

import android.graphics.Bitmap
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.test.espresso.Espresso
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
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

    @Test fun boxedTablesInTheConsole() {
        val model = (compose.activity.application as HardchargerApplication).model
        compose.waitUntil(120_000) { model.running && !model.busy }
        compose.onNodeWithContentDescription("Console", useUnmergedTree = true).performClick()
        compose.onNodeWithContentDescription("Clear console").performClick()
        fun run(sql: String, expect: String) {
            compose.onNodeWithText("SQL or .help").performTextInput(sql)
            compose.onNodeWithText("Run", useUnmergedTree = true).performClick()
            compose.waitUntil(30_000) {
                !model.querying && runCatching { compose.onNodeWithText(expect, substring = true).assertExists() }.isSuccess
            }
        }
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
