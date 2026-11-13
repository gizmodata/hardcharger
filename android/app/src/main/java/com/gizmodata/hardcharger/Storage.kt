package com.gizmodata.hardcharger

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.KeyStore
import java.security.SecureRandom
import java.text.Normalizer
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Credentials are encrypted with a non-exportable Android Keystore key; backups are disabled. */
class Vault(private val context: Context) {
    private val file = File(context.noBackupFilesDir, "credentials.enc")
    private val alias = "hardcharger.credentials.v1"
    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
        }.generateKey()
    }
    @Synchronized fun read(): JSONObject {
        if (!file.exists()) return JSONObject()
        val bytes = file.readBytes()
        require(bytes.size > 28) { "Stored credentials are damaged." }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, bytes.copyOfRange(0, 12)))
        return JSONObject(String(cipher.doFinal(bytes.copyOfRange(12, bytes.size)), Charsets.UTF_8))
    }
    @Synchronized fun write(value: JSONObject) {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val pending = File(file.parentFile, "credentials.pending")
        pending.outputStream().use { stream ->
            stream.write(cipher.iv); stream.write(cipher.doFinal(value.toString().toByteArray(Charsets.UTF_8)))
            stream.fd.sync()
        }
        check(pending.renameTo(file)) { "Could not save credentials." }
    }
    @Synchronized fun local(): Pair<String, String> {
        val data = read()
        if (!data.has("password")) {
            data.put("password", randomSecret()).put("secret", randomSecret())
            write(data)
        }
        return data.getString("password") to data.getString("secret")
    }
    fun remotes(): List<RemoteServer> {
        val array = read().optJSONArray("servers") ?: JSONArray()
        return (0 until array.length()).map { RemoteServer.fromJson(array.getJSONObject(it)) }
    }
    fun saveRemotes(servers: List<RemoteServer>) {
        write(read().put("servers", JSONArray().apply { servers.forEach { put(it.json()) } }))
    }
    @Synchronized fun rotateLocal() {
        write(read().put("password", randomSecret()).put("secret", randomSecret()))
    }
    private fun randomSecret(): String = Base64.encodeToString(ByteArray(32).also { SecureRandom().nextBytes(it) }, Base64.NO_WRAP or Base64.URL_SAFE)
}

data class ServerSettings(
    val port: Int = 31337, val memoryMB: Int = 0, val threads: Int = 0,
    val autoStart: Boolean = true, val share: Boolean = false, val tls: Boolean = false,
    val keepAwake: Boolean = true, val logLevel: String = "info", val printQueries: Boolean = true,
) {
    // Zero persists the automatic choice; resolve it only for display and server startup.
    fun resolvedMemoryMB(physicalMemoryBytes: Long): Int = if (memoryMB == 0)
        (physicalMemoryBytes.coerceAtLeast(0) / (1024 * 1024) * 30 / 100).coerceIn(256, 4096).toInt() else memoryMB
    fun resolvedThreads(processors: Int): Int = if (threads == 0) processors.coerceAtLeast(1) else threads
    val usesTLS get() = share || tls
    fun validate() {
        require(port in 1024..65535) { "Port must be between 1024 and 65535." }
        require(memoryMB == 0 || memoryMB in 128..8192) { "Choose Automatic or a memory limit between 128 and 8192 MB." }
        require(threads in 0..64) { "Choose One per core or between 1 and 64 threads." }
        require(logLevel in listOf("debug", "info", "warning", "error")) { "Unknown log level." }
    }
    fun save(context: Context) {
        validate()
        context.getSharedPreferences("settings", Context.MODE_PRIVATE).edit()
            .putInt("port", port).putInt("memory", memoryMB).putInt("threads", threads)
            .putBoolean("autoStart", autoStart).putBoolean("share", share).putBoolean("tls", tls)
            .putBoolean("keepAwake", keepAwake).putString("logLevel", logLevel).putBoolean("printQueries", printQueries).apply()
    }
    companion object {
        fun load(context: Context): ServerSettings = context.getSharedPreferences("settings", Context.MODE_PRIVATE).let {
            ServerSettings(it.getInt("port", 31337), it.getInt("memory", 0), it.getInt("threads", 0),
                it.getBoolean("autoStart", true), it.getBoolean("share", false), it.getBoolean("tls", false),
                it.getBoolean("keepAwake", true), it.getString("logLevel", "info")!!, it.getBoolean("printQueries", true))
        }
    }
}

class AppPaths(context: Context) {
    val physicalMemoryBytes = android.app.ActivityManager.MemoryInfo().also {
        context.getSystemService(android.app.ActivityManager::class.java).getMemoryInfo(it)
    }.totalMem
    val processorCount = Runtime.getRuntime().availableProcessors().coerceAtLeast(1)
    val root = context.filesDir
    val database = File(root, "Hardcharger.duckdb")
    val imports = File(root, "Imports").apply { mkdirs() }
    val exports = File(root, "Exports").apply { mkdirs() }
    val private = File(context.noBackupFilesDir, "private").apply { mkdirs() }
    val certificate = File(private, "server.crt")
    val ca = File(private, "cacert.pem").apply { if (!exists()) context.assets.open("cacert.pem").use { input -> outputStream().use { input.copyTo(it) } } }
    val spill = File(context.cacheDir, "duckdb-spill").apply { mkdirs() }
    val log = File(root, "hardcharger.log")
    val sensitive = listOf(context.noBackupFilesDir.path,
        File(context.applicationInfo.dataDir, "shared_prefs").path,
        File(context.applicationInfo.dataDir, "app_webview").path,
        File(context.applicationInfo.dataDir, "databases").path)
    fun arguments(settings: ServerSettings): Array<String> = buildList {
        add("hardcharger-server")
        add("--host=${if (settings.share) "0.0.0.0" else "127.0.0.1"}")
        add("--port=${settings.port}")
        add("--database=${database.path}"); add("--web-ui=true"); add("--admin-username=admin")
        add("--log-file=${log.path}"); add("--log-level=${settings.logLevel}")
        add("--print-queries=${settings.printQueries}"); add("--memory-limit=${settings.resolvedMemoryMB(physicalMemoryBytes)}MB")
        add("--graceful-shutdown=true"); add("--shutdown-grace-period-seconds=5")
        add("--sensitive-paths=${sensitive.joinToString(",")}")
        add("--init-sql-commands=SET GLOBAL temp_directory=${sqlString(spill.path)}; SET GLOBAL threads=${settings.resolvedThreads(processorCount)};")
        if (settings.usesTLS) { add("--tls-cert=${certificate.path}"); add("--tls-key=${File(private, "server.key").path}") }
    }.toTypedArray()
}

fun sqlString(value: String) = "'${value.replace("'", "''")}'"
fun sqlIdentifier(value: String) = "\"${value.replace("\"", "\"\"")}\""
fun tableName(filename: String): String {
    val normalized = Normalizer.normalize(filename.substringBeforeLast('.', filename), Normalizer.Form.NFKD)
        .replace(Regex("\\p{M}"), "").lowercase().replace(Regex("[^a-z0-9]+"), "_").trim('_').ifEmpty { "imported" }
    return if (normalized.first().isDigit()) "t_$normalized" else normalized
}
fun importReader(name: String): String = when (name.substringAfterLast('.').lowercase()) {
    "csv", "tsv", "tab", "txt", "psv" -> "read_csv"
    "json", "jsonl", "ndjson" -> "read_json_auto"
    "parquet", "pq" -> "read_parquet"
    else -> error("Choose a CSV, TSV, JSON or Parquet file. For Excel, use Upload in the workspace.")
}
