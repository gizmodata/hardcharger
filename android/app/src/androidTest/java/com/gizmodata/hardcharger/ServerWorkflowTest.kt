package com.gizmodata.hardcharger

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.core.content.FileProvider
import kotlinx.coroutines.*
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

@RunWith(AndroidJUnit4::class)
class ServerWorkflowTest {
    @Test fun nativeServerConsolePersistenceImportAndTLS() = runBlocking {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val model = (context.applicationContext as HardchargerApplication).model
        withTimeout(120_000) { while (!withContext(Dispatchers.Main) { model.running && !model.busy }) delay(100) }
        withContext(Dispatchers.Main) {
            model.stop()
            model.saveSettings(ServerSettings(port = 31337, autoStart = true))
            model.start()
            assertTrue(model.running)
            val memoryMB = model.settings.resolvedMemoryMB(model.paths.physicalMemoryBytes)
            val threads = model.settings.resolvedThreads(model.paths.processorCount)
            // Read the real DuckDB settings, including the units it reports after rounding.
            val effective = withContext(Dispatchers.IO) {
                NativeBridge.runConsole(".mode csv\nSELECT current_setting('threads') AS threads, current_setting('memory_limit') AS memory_limit;")
            }
            val values = Regex("""(?m)^(\d+),([0-9.]+) (KiB|MiB|GiB|TiB|B)\r?$""").find(effective)
                ?: error("Unexpected resource settings output: $effective")
            assertEquals(threads, values.groupValues[1].toInt())
            val unit = when (values.groupValues[3]) {
                "KiB" -> 1024.0; "MiB" -> 1048576.0; "GiB" -> 1073741824.0; "TiB" -> 1099511627776.0; else -> 1.0
            }
            val effectiveBytes = values.groupValues[2].toDouble() * unit
            val requestedBytes = memoryMB * 1_000_000.0 // DuckDB's MB setting is decimal.
            assertEquals(requestedBytes, effectiveBytes, requestedBytes * 0.06)
            model.run("SELECT 42 AS answer;")
            assertTrue(model.transcript.contains("42"))
            model.run("SELECT '😀 café 東京' AS unicode;")
            assertTrue(model.transcript.contains("😀 café 東京"))
            model.run("CREATE OR REPLACE TABLE android_persistence AS SELECT 73 AS value;")
            model.restart()
            model.run("SELECT value FROM android_persistence;")
            assertTrue(model.transcript.contains("73"))
            val longQuery = launch { model.run("SELECT sum(a.i * b.i) FROM range(1000000000) a(i), range(1000000000) b(i);") }
            withTimeout(10_000) { while (!model.querying) delay(20) }
            delay(500)
            model.cancel()
            withTimeout(15_000) { longQuery.join() }
            assertFalse(model.querying)
            model.run("SELECT 127 AS after_cancel;")
            assertTrue(model.transcript.contains("127"))
            assertFalse(NativeBridge.complete("SELECT ';'"))
            assertTrue(NativeBridge.complete("SELECT ';';"))
            assertTrue(NativeBridge.complete(".tables"))
            model.run(".mode csv\nSELECT 42 AS answer;")
            assertTrue(model.transcript.contains("answer\n42"))
            val file = File(model.paths.exports, "android_test_${System.nanoTime()}.csv")
            withContext(Dispatchers.IO) { file.writeText("id,name\n1,Ada\n2,Grace\n") }
            model.importFile(FileProvider.getUriForFile(context, "com.gizmodata.hardcharger.files", file))
            model.run("SELECT count(*) AS imported FROM ${sqlIdentifier(tableName(file.name))};")
            assertTrue(model.transcript.contains("imported\n2"))
            model.message = null
            model.saveSettings(model.settings.copy(tls = true))
            model.restart()
            assertTrue(model.connection!!.endpoint.tls)
            model.run("SELECT 19 AS tls_answer;")
            assertTrue(model.transcript.contains("19"))
            val endpoint = model.connection!!.endpoint
            withContext(Dispatchers.IO) {
                val anonymous = ChargewireClient(endpoint.copy(certificatePEM = ""))
                try { anonymous.discover(); fail("Unapproved self-signed certificate was accepted") }
                catch (_: CertificateApproval) { }
                val trusted = ChargewireClient(endpoint)
                assertEquals("chargewire", trusted.discover().getString("protocol"))
            }
            model.saveSettings(model.settings.copy(tls = false))
            model.restart()
            assertTrue(model.running)
            val previousPassword = model.localPassword()
            model.regeneratePassword()
            assertNotEquals(previousPassword, model.localPassword())
            withContext(Dispatchers.IO) {
                try { ChargewireClient(model.connection!!.endpoint).signIn("admin", previousPassword); fail("Old password still works") }
                catch (expected: IllegalStateException) { }
            }
            model.message = null
            model.stop()
            val occupied = java.net.ServerSocket(0)
            try {
                model.saveSettings(model.settings.copy(port = occupied.localPort))
                try { model.start(); fail("An occupied port started") } catch (expected: Exception) { assertFalse(model.running) }
            } finally { occupied.close(); model.saveSettings(model.settings.copy(port = 31337)); model.start() }
        }
    }
    @Test fun encryptedVaultAndSettingsRoundTrip() = runBlocking {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val vault = Vault(context)
        val original = vault.local()
        assertEquals(original, Vault(context).local())
        assertFalse(File(context.noBackupFilesDir, "credentials.enc").readText().contains(original.first))
        val server = RemoteServer(name = "Test connection", endpoint = Endpoint("https://db.example:31337"), username = "user", password = "test-only-password")
        val previous = vault.remotes()
        try { vault.saveRemotes(previous + server); assertEquals(server, Vault(context).remotes().last()) }
        finally { vault.saveRemotes(previous) }
    }
    @Test fun connectionLinksRequireReviewAndValidPins() {
        val pin = List(32) { "AB" }.joinToString(":")
        val uri = android.net.Uri.Builder().scheme("hardcharger").authority("connect")
            .appendQueryParameter("url", "https://example.com:31337/ui/")
            .appendQueryParameter("name", "My server").appendQueryParameter("fingerprint", pin).build()
        val remote = connectionLink(uri)
        assertEquals("https://example.com:31337", remote.endpoint.origin)
        assertEquals("hardcharger", remote.username)
        assertEquals(pin, remote.expectedFingerprint)
        assertEquals("", remote.password)
        assertEquals("", remote.endpoint.certificatePEM)
        try { connectionLink(uri.buildUpon().appendQueryParameter("unused", "value").encodedQuery("url=http%3A%2F%2Fexample.com&fingerprint=AB").build()); fail("Bad pin accepted") }
        catch (_: IllegalArgumentException) { }
    }
}
