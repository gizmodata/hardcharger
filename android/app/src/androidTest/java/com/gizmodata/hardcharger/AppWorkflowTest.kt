package com.gizmodata.hardcharger

import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class AppWorkflowTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()
    @Test fun tabletRailDestinationsRemainReachableFromAbout() {
        org.junit.Assume.assumeTrue(compose.activity.resources.configuration.screenWidthDp >= 720)
        val model = (compose.activity.application as HardchargerApplication).model
        compose.waitUntil(120_000) { model.running && !model.busy }
        fun open(label: String) {
            compose.onNodeWithContentDescription(label, useUnmergedTree = true)
                .performScrollTo().assertIsDisplayed().performTouchInput { click() }
        }
        fun about() {
            open("Settings")
            compose.onNodeWithText("Port").assertExists()
            compose.onNodeWithText("About Hardcharger™").performScrollTo().performClick()
            compose.onNodeWithText("About").assertIsDisplayed()
        }
        about()
        compose.activityRule.scenario.recreate()
        // About's app bar reduces the rail's available height. Exercise the bottom
        // destinations with real touch input, then scroll back to the top destination.
        open("Saved servers")
        compose.onNodeWithText("Your servers").assertIsDisplayed()
        about()
        open("Logs")
        compose.onNodeWithText("Follow New Lines").assertIsDisplayed()
        about()
        open("Console")
        compose.onNodeWithText("SQL or .help").assertExists()
        about()
        open("Web UI")
        compose.onNodeWithText("Reconnect").assertExists()
        about()
        open("Server")
        compose.onNodeWithContentDescription("Server status: Running").assertIsDisplayed()
    }

    @Test fun automaticResourceControlsOfferFixedAndCustomChoices() {
        val model = (compose.activity.application as HardchargerApplication).model
        val original = model.settings
        compose.onNodeWithContentDescription("Settings", useUnmergedTree = true).performClick()
        compose.onNodeWithContentDescription("Memory limit setting").performClick()
        compose.onAllNodesWithText("Automatic").onLast().performClick()
        compose.onNodeWithText("Automatic").assertIsDisplayed()
        compose.onNodeWithContentDescription("Threads setting").performClick()
        compose.onAllNodesWithText("One per core").onLast().performClick()
        compose.onNodeWithText("One per core").assertIsDisplayed()
        compose.activityRule.scenario.recreate()
        compose.onNodeWithText("Automatic").assertIsDisplayed()
        compose.onNodeWithText("One per core").assertIsDisplayed()
        compose.onNodeWithContentDescription("Memory limit setting").performClick()
        compose.onNodeWithText("1 GB").performClick()
        compose.onNodeWithText("1 GB").assertIsDisplayed()
        compose.onNodeWithContentDescription("Memory limit setting").performClick()
        compose.onNodeWithText("Custom…").performClick()
        compose.onNodeWithContentDescription("Custom limit (MB)").performTextReplacement("384")
        assertTrue(model.settings == original) // Resource edits still require Save settings.
        compose.onNodeWithContentDescription("Server", useUnmergedTree = true).performClick()
    }
    @Test fun emojiLogPreferenceIsSharedAndDoesNotRestartServer() {
        val model = (compose.activity.application as HardchargerApplication).model
        compose.waitUntil(120_000) { model.running && !model.busy }
        val original = model.logStyle
        val startedAt = model.startedAt
        try {
            compose.runOnIdle { model.updateLogStyle(LogStyle.Emoji) }
            compose.onNodeWithContentDescription("Settings", useUnmergedTree = true).performClick()
            compose.onNodeWithText("Log view").performScrollTo().assertIsDisplayed()
            compose.onNodeWithText("Emoji").performClick()
            compose.onNodeWithText("Plain").performClick()
            compose.onNodeWithContentDescription("Server", useUnmergedTree = true).performClick()
            compose.onNodeWithText("Server Log").performScrollTo().performClick()
            compose.onNodeWithText("Plain").assertIsSelected()
            compose.onNodeWithText("Filter").performTextInput("Hardcharger is ready")
            compose.waitUntil(20_000) { compose.onAllNodesWithText("Hardcharger is ready", substring = true).fetchSemanticsNodes().isNotEmpty() }
            compose.onAllNodesWithText("✅ info").assertCountEquals(0)
            compose.onNodeWithText("Emoji").performClick()
            compose.waitUntil(20_000) { compose.onAllNodesWithText("✅ info").fetchSemanticsNodes().isNotEmpty() }
            compose.onNodeWithContentDescription("Settings", useUnmergedTree = true).performClick()
            compose.onNodeWithText("Log view").performScrollTo()
            compose.onNodeWithText("Emoji").assertIsDisplayed()
            compose.activityRule.scenario.recreate()
            compose.onNodeWithText("Log view").performScrollTo()
            compose.onNodeWithText("Emoji").assertIsDisplayed()
            assertTrue(model.startedAt == startedAt && model.running)
            assertTrue(model.context.getSharedPreferences("appearance", android.content.Context.MODE_PRIVATE).getString("logStyle", null) == "emoji")
        } finally { compose.runOnIdle { model.updateLogStyle(original) } }
    }
    @Test fun consoleNavigationRestartConfirmationAndSettings() {
        val model = (compose.activity.application as HardchargerApplication).model
        compose.waitUntil(120_000) { model.running && !model.busy }
        compose.onNodeWithContentDescription("Server status: Running").assertIsDisplayed()
        compose.onNodeWithText("Stop", useUnmergedTree = true).performClick()
        compose.onNodeWithText("Stop this server?").assertIsDisplayed()
        compose.onNodeWithText("Cancel").performClick()
        assertTrue(model.running)
        compose.onNodeWithContentDescription("Console", useUnmergedTree = true).performClick()
        compose.onNodeWithText("SQL or .help").performTextInput("SELECT 4242 AS android_ui_answer;")
        compose.onNodeWithText("Run", useUnmergedTree = true).performClick()
        compose.waitUntil(30_000) { model.transcript.contains("4242") && !model.querying }
        compose.onNodeWithText("4242", substring = true).assertIsDisplayed()
        compose.onNodeWithContentDescription("Previous command").performClick()
        compose.onNodeWithText("SQL or .help").assertTextContains("SELECT 4242 AS android_ui_answer;")
        compose.onNodeWithContentDescription("Next command").performClick()
        compose.onNodeWithContentDescription("Clear input").assertIsNotEnabled()
        compose.onNodeWithContentDescription("Settings", useUnmergedTree = true).performClick()
        compose.onNodeWithText("Port").assertIsDisplayed()
        compose.activityRule.scenario.recreate()
        compose.onNodeWithText("Port").assertIsDisplayed()
        compose.onNodeWithText("Saved Servers").performScrollTo().performClick()
        compose.onNodeWithText("Your servers").assertIsDisplayed()
        compose.onNodeWithContentDescription("Back").performClick()
        compose.onNodeWithText("Port").assertExists()
        compose.onNodeWithContentDescription("Server", useUnmergedTree = true).performClick()
        compose.onNodeWithContentDescription("Server status: Running").assertIsDisplayed()
        compose.onNodeWithText("Server Log").performScrollTo().performClick()
        compose.onNodeWithText("Follow New Lines").assertIsDisplayed()
        compose.onNodeWithText("Filter").performTextInput("no_matching_log_line_427829")
        compose.onNodeWithText("No matching lines").assertIsDisplayed()
        compose.onNodeWithText("Filter").performTextClearance()
        compose.onNodeWithContentDescription("Back").performClick()
        compose.onNodeWithText("Share on Local Network").performScrollTo().performClick()
        compose.onNodeWithText("Administrator").assertExists()
        compose.onNodeWithText("Done").performClick()
    }

    @Test fun aboutVersionsNoticesAndBackNavigation() {
        compose.onNodeWithContentDescription("Settings", useUnmergedTree = true).performClick()
        compose.onNodeWithText("About Hardcharger™").performScrollTo().performClick()
        compose.onNodeWithContentDescription("Hardcharger™").assertIsDisplayed()
        compose.onNodeWithText("App").performScrollTo().assertIsDisplayed()
        compose.onNodeWithText("${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})").assertIsDisplayed()
        val versions = NativeBridge.versionDetails()
        assertTrue(versions.size == 4 && versions.all { it.isNotBlank() })
        compose.onNodeWithText("Hardcharger™ server").assertExists()
        compose.activityRule.scenario.recreate()
        compose.onNodeWithText("About").assertIsDisplayed()
        compose.onNodeWithText("Hardcharger™ and GizmoData™ are trademarks of GizmoData LLC.").performScrollTo().assertIsDisplayed()
        compose.onNodeWithContentDescription("by GizmoData™").assertIsDisplayed()
        compose.onNodeWithText("Third-Party Software").performScrollTo().performClick()
        compose.waitUntil(20_000) { compose.onAllNodesWithText("Hardcharger for Android").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("Hardcharger for Android").assertIsDisplayed()
        androidx.test.espresso.Espresso.pressBack()
        compose.onNodeWithText("About").assertIsDisplayed()
        compose.onNodeWithText("Privacy Policy").performScrollTo().performClick()
        compose.waitUntil(20_000) { compose.onAllNodesWithText("Hardcharger for Android — Privacy Policy").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("Hardcharger for Android — Privacy Policy").assertIsDisplayed()
        compose.onNodeWithContentDescription("Back").performClick()
        compose.onNodeWithText("About").assertIsDisplayed()
        compose.onNodeWithContentDescription("Back").performClick()
        compose.onNodeWithText("Port").assertExists()
    }
}
