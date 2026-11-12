package com.gizmodata.hardcharger

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.coroutines.*
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class PlayDistributionTest {
    @Test fun localDefaultsKeepSignedExtensionSupport() = runBlocking {
        assumeTrue(BuildConfig.DISTRIBUTION == "local")
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val model = (context.applicationContext as HardchargerApplication).model
        withTimeout(120_000) { while (!withContext(Dispatchers.Main) { model.running && !model.busy }) delay(100) }
        assertEquals("local", NativeBridge.distribution())
        val defaults = withContext(Dispatchers.IO) {
            NativeBridge.runConsole(".mode csv\nSELECT current_setting('autoinstall_known_extensions'), current_setting('autoload_known_extensions'), current_setting('allow_unsigned_extensions');")
        }
        assertTrue(defaults, defaults.contains("true,true,false"))
    }

    @Test fun externalNativeCodeIsAbsentEvenWhenSettingsAreEnabled() = runBlocking {
        assumeTrue(BuildConfig.DISTRIBUTION == "play")
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val model = (context.applicationContext as HardchargerApplication).model
        withTimeout(120_000) { while (!withContext(Dispatchers.Main) { model.running && !model.busy }) delay(100) }
        assertEquals("play", NativeBridge.distribution())
        withContext(Dispatchers.IO) {
            val defaults = NativeBridge.runConsole(".mode csv\nSELECT current_setting('autoinstall_known_extensions') AS install, current_setting('autoload_known_extensions') AS load;")
            assertTrue(defaults, defaults.contains("false,false"))
            val bundled = NativeBridge.runConsole("SELECT extension_name FROM duckdb_extensions() WHERE loaded ORDER BY extension_name;")
            for (name in listOf("autocomplete", "tpch", "httpfs", "ducklake", "postgres_scanner", "core_functions", "parquet", "json", "icu")) {
                assertTrue("Missing $name: $bundled", bundled.lineSequence().any { it.trim() == name })
            }
            // Restoring settings cannot restore code excluded from the library.
            NativeBridge.runConsole("SET home_directory='${context.filesDir.absolutePath.replace("'", "''")}';")
            NativeBridge.runConsole("SET autoinstall_known_extensions=true; SET autoload_known_extensions=true;")
            try {
                val install = NativeBridge.runConsole("INSTALL 'http://127.0.0.1:9/forbidden.duckdb_extension';")
                assertTrue(install, install.contains("Installing external extensions is not supported"))
                val load = NativeBridge.runConsole("LOAD '/data/local/tmp/forbidden.duckdb_extension';")
                assertTrue(load, load.contains("Loading external extensions is not supported"))
                val json = NativeBridge.runConsole("SELECT json_extract('{\"answer\":42}', '$.answer') AS answer;")
                assertTrue(json, json.contains("answer\n42"))
            } finally {
                NativeBridge.runConsole("SET autoinstall_known_extensions=false; SET autoload_known_extensions=false;")
            }
        }
    }
}
