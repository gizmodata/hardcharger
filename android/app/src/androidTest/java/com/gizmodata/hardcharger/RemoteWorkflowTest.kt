package com.gizmodata.hardcharger

import android.os.ParcelFileDescriptor
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.*
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RemoteWorkflowTest {
    private fun fixture(): JSONObject {
        val descriptor = InstrumentationRegistry.getInstrumentation().uiAutomation.executeShellCommand("cat /data/local/tmp/hardcharger-remote.json")
        val text = ParcelFileDescriptor.AutoCloseInputStream(descriptor).bufferedReader().use { it.readText() }
        assumeTrue("Start android/scripts/remote-fixture.py for remote/SSO tests", text.trim().startsWith("{"))
        return JSONObject(text)
    }
    @Test fun passwordAndBrowserSignInToADifferentServer() = runBlocking {
        val data = fixture()
        val endpoint = Endpoint.parse(data.getString("origin"))
        val client = ChargewireClient(endpoint)
        val discovery = withContext(Dispatchers.IO) { client.discover() }
        assertTrue(discovery.getString("protocol") in listOf("chargewire", "warp-sql"))
        val token = withContext(Dispatchers.IO) { client.signIn(data.getString("username"), data.getString("password")) }
        assertTrue(token.fresh())
        val flow = withContext(Dispatchers.IO) { client.beginBrowser() }
        // The fixture identity provider validates nonce, PKCE and client credentials,
        // then redirects exactly as a completed browser sign-in does.
        withContext(Dispatchers.IO) {
            OkHttpClient().newCall(Request.Builder().url(flow.url).build()).execute().use { assertTrue(it.isSuccessful) }
        }
        val browserToken = withTimeout(15_000) {
            var result: AccessToken? = null
            while (result == null) { result = withContext(Dispatchers.IO) { client.pollBrowser(flow) }; if (result == null) delay(200) }
            result
        }
        assertTrue(browserToken.fresh())
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val model = (context.applicationContext as HardchargerApplication).model
        withTimeout(120_000) { while (!withContext(Dispatchers.Main) { model.running && !model.busy }) delay(100) }
        val remote = RemoteServer(name = "Remote fixture", endpoint = endpoint, username = data.getString("username"), password = data.getString("password"))
        withContext(Dispatchers.Main) {
            try {
                model.saveRemote(remote)
                model.connectRemote(remote)
                assertEquals(remote.id, model.connection?.remoteID)
                model.run("SELECT 8181 AS remote_answer;")
                assertTrue(model.transcript.contains("8181"))
            } finally { model.deleteRemote(remote); model.connectLocal() }
        }
    }
    @Test fun browserCustomTabCompletesSignIn() = runBlocking {
        // Opt-in: the emulator's browser first-run screens must be completed first.
        assumeTrue(InstrumentationRegistry.getArguments().getString("browserUI") == "true")
        val data = fixture()
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val model = (context.applicationContext as HardchargerApplication).model
        val remote = RemoteServer(name = "Browser UI fixture", endpoint = Endpoint.parse(data.getString("origin")), browser = true)
        val scenario = androidx.test.core.app.ActivityScenario.launch(MainActivity::class.java)
        try {
            withTimeout(120_000) { while (!withContext(Dispatchers.Main) { model.running && !model.busy }) delay(100) }
            withContext(Dispatchers.Main) { model.saveRemote(remote); model.connectRemote(remote) }
            withTimeout(60_000) { while (!withContext(Dispatchers.Main) { model.connection?.remoteID == remote.id }) delay(200) }
            withContext(Dispatchers.Main) {
                assertNull(model.browserURL) // Activity consumed it and launched the Custom Tab.
                // Reconnect must await a fresh browser token before accepting the next query.
                // Bring the activity back so its effect can open the next Custom Tab.
                context.startActivity(android.content.Intent(context, MainActivity::class.java)
                    .addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK))
                withTimeout(20_000) { model.refreshConnection() }
                assertEquals(remote.id, model.connection?.remoteID)
                model.run("SELECT 9191 AS browser_ui_answer;")
                assertTrue(model.transcript.contains("9191"))
            }
        } finally {
            withContext(Dispatchers.Main) { model.cancelSignIn(); model.deleteRemote(remote); if (model.running) model.connectLocal() }
            scenario.close()
        }
    }
}
