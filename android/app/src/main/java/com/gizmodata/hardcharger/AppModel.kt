package com.gizmodata.hardcharger

import android.app.Application
import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.ProcessLifecycleOwner
import kotlinx.coroutines.*
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.io.File

class HardchargerApplication : Application() {
    lateinit var model: AppModel
    override fun onCreate() {
        super.onCreate()
        check(NativeBridge.distribution() == BuildConfig.DISTRIBUTION) { "The packaged native library does not match this distribution." }
        model = AppModel(this)
        ProcessLifecycleOwner.get().lifecycle.addObserver(model)
    }
}

data class Connection(val label: String, val endpoint: Endpoint, val token: AccessToken, val remoteID: String? = null)
class AppModel(val context: Context) : DefaultLifecycleObserver {
    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    val paths = AppPaths(context)
    private val vault = Vault(context)
    private val advertisement = NetworkAdvertisement(context)
    private val serverLock = Mutex()
    private val consoleLock = Mutex()
    var settings by mutableStateOf(ServerSettings.load(context)); private set
    // Presentation preferences apply immediately and never restart the embedded server.
    private val appearance = context.getSharedPreferences("appearance", Context.MODE_PRIVATE)
    var logStyle by mutableStateOf(if (appearance.getString("logStyle", "emoji") == "plain") LogStyle.Plain else LogStyle.Emoji); private set
    fun updateLogStyle(style: LogStyle) {
        appearance.edit().putString("logStyle", style.name.lowercase()).apply()
        logStyle = style
    }
    var runningSettings by mutableStateOf<ServerSettings?>(null); private set
    var remotes by mutableStateOf<List<RemoteServer>>(emptyList()); private set
    var running by mutableStateOf(false); private set
    var busy by mutableStateOf(false); private set
    var querying by mutableStateOf(false); private set
    var connection by mutableStateOf<Connection?>(null); private set
    var error by mutableStateOf<String?>(null)
    var message by mutableStateOf<String?>(null)
    var transcript by mutableStateOf(""); private set
    var history by mutableStateOf<List<String>>(emptyList()); private set
    var startedAt by mutableStateOf(0L); private set
    var localPort by mutableStateOf(0); private set
    var browserURL by mutableStateOf<String?>(null)
    var signingIn by mutableStateOf(false); private set
    var pendingTrust by mutableStateOf<Pair<RemoteServer, String>?>(null)
    var pendingConnection by mutableStateOf<RemoteServer?>(null)
    var webGeneration by mutableStateOf(0); private set
    var documentPickerActive = false
    private var signInJob: Job? = null
    private var backgroundJob: Job? = null
    private var resumeLocal = false
    private var loaded = false
    val versions = NativeBridge.version()

    init {
        scope.launch {
            attempt {
                withContext(Dispatchers.IO) { vault.local(); remotes = vault.remotes() }
                loaded = true
                if (settings.autoStart) start()
            }
        }
        scope.launch {
            while (isActive) {
                delay(3000)
                if (running && !busy && !withContext(Dispatchers.IO) { NativeBridge.running() }) {
                    attempt { stop() }
                    error = "The local server stopped unexpectedly. Check Logs, then start it again."
                }
            }
        }
    }
    suspend fun attempt(block: suspend () -> Unit) {
        try { block() } catch (cancelled: CancellationException) { throw cancelled }
        catch (failure: Exception) { error = failure.message ?: failure.javaClass.simpleName }
    }
    fun launch(block: suspend () -> Unit) = scope.launch { attempt(block) }
    override fun onStart(owner: LifecycleOwner) {
        backgroundJob?.cancel()
        if (resumeLocal && loaded) { resumeLocal = false; launch { start() } }
    }
    override fun onStop(owner: LifecycleOwner) {
        // A foreground-only local server. Leave time for Android's document picker/share UI.
        backgroundJob = scope.launch {
            delay(if (documentPickerActive) 300_000 else 30_000)
            if (running) { resumeLocal = true; attempt { stop() } }
        }
    }
    suspend fun start() = serverLock.withLock {
        if (running) return@withLock
        busy = true
        try {
            val snapshot = settings
            val port = withContext(Dispatchers.IO) {
                snapshot.validate()
                if (paths.log.length() > 8 * 1024 * 1024) {
                    val old = File(paths.root, "hardcharger.previous.log")
                    old.delete(); paths.log.renameTo(old)
                }
                if (snapshot.usesTLS) ensureCertificate()
                val credential = vault.local()
                NativeBridge.start(paths.arguments(snapshot), credential.first, credential.second, paths.ca.path)
            }
            localPort = port; runningSettings = snapshot; running = true; startedAt = System.currentTimeMillis()
            if (snapshot.share) advertisement.start(port)
            if (connection?.remoteID == null) connectLocal()
        } finally { busy = false }
    }
    suspend fun stop() = withContext(NonCancellable) { serverLock.withLock {
        busy = true
        try {
            advertisement.stop()
            if (connection?.remoteID == null) {
                connection = null; webGeneration++
                withContext(Dispatchers.IO) { NativeBridge.cancelConsole() }
                consoleLock.withLock { withContext(Dispatchers.IO) { NativeBridge.closeConsole() } }
            }
            withContext(Dispatchers.IO) { NativeBridge.stop() }
            running = false; runningSettings = null
        } finally { busy = false }
    }
    }
    suspend fun restart() { stop(); start() }
    suspend fun connectLocal() {
        check(running) { "Start the on-device server first." }
        val currentSettings = runningSettings ?: settings
        val endpoint = Endpoint("${if (currentSettings.usesTLS) "https" else "http"}://127.0.0.1:$localPort",
            if (currentSettings.usesTLS) withContext(Dispatchers.IO) { paths.certificate.readText() } else "")
        val token = withContext(Dispatchers.IO) { ChargewireClient(endpoint).signIn("admin", vault.local().first) }
        attach(Connection("This device", endpoint, token))
    }
    private suspend fun attach(next: Connection) = consoleLock.withLock {
        withContext(Dispatchers.IO) {
            val endpoint = next.endpoint
            val roots = if (endpoint.certificatePEM.isNotEmpty()) File(paths.private, "console-ca.pem").apply { writeText(endpoint.certificatePEM) }.path else paths.ca.path
            NativeBridge.closeConsole()
            NativeBridge.openConsole(endpoint.url.host, endpoint.url.port, endpoint.tls, roots, next.token.username, "", next.token.token)
        }
        connection = next; transcript = "Connected to ${next.label}. Type .help for console commands.\n"; webGeneration++
    }
    suspend fun run(input: String) {
        if (input.isBlank() || querying) return
        if (connection?.token?.fresh() == false) refreshConnection()
        querying = true
        try {
            consoleLock.withLock {
                history = (history.filterNot { it == input } + input).takeLast(100)
                transcript = (transcript + "\n› $input\n").takeLast(200_000)
                val output = withContext(Dispatchers.IO) { NativeBridge.runConsole(input) }
                transcript = (transcript + output).takeLast(200_000)
            }
        } finally { querying = false }
    }
    fun cancel() { scope.launch(Dispatchers.IO) { NativeBridge.cancelConsole() } }
    fun clearConsole() { transcript = "" }
    suspend fun refreshConnection() {
        val active = connection ?: return
        if (active.remoteID == null) connectLocal() else {
            val remote = remotes.first { it.id == active.remoteID }
            connectRemote(remote)
            if (remote.browser) signInJob?.join()
            check(connection?.token?.fresh() == true) { "Sign-in was not completed. Reconnect to the server and try again." }
        }
    }
    suspend fun saveSettings(next: ServerSettings) { next.validate(); withContext(Dispatchers.IO) { next.save(context) }; settings = next }
    suspend fun saveRemote(remote: RemoteServer) {
        require(remote.name.isNotBlank()) { "Give this server a name." }
        val next = (remotes.filterNot { it.id == remote.id } + remote).sortedBy { it.name.lowercase() }
        withContext(Dispatchers.IO) { vault.saveRemotes(next) }; remotes = next
    }
    suspend fun deleteRemote(remote: RemoteServer) {
        val next = remotes.filterNot { it.id == remote.id }
        withContext(Dispatchers.IO) { vault.saveRemotes(next) }; remotes = next
        if (connection?.remoteID == remote.id) {
            connection = null; webGeneration++
            consoleLock.withLock { withContext(Dispatchers.IO) { NativeBridge.closeConsole() } }
        }
    }
    suspend fun connectRemote(remote: RemoteServer) {
        signingIn = true
        try {
            val client = ChargewireClient(remote.endpoint)
            withContext(Dispatchers.IO) { client.discover() }
            require(remote.expectedFingerprint.isEmpty() || remote.expectedFingerprint == client.presentedFingerprint()) {
                "The certificate does not match the connection link. Verify it with the server owner."
            }
            if (remote.browser) {
                val flow = withContext(Dispatchers.IO) { client.beginBrowser() }
                browserURL = flow.url
                signInJob?.cancel()
                signInJob = scope.launch {
                    try {
                        withTimeout(300_000) {
                            while (true) {
                                delay(flow.intervalMillis)
                                val token = withContext(Dispatchers.IO) { client.pollBrowser(flow) } ?: continue
                                attach(Connection(remote.name, remote.endpoint, token, remote.id)); break
                            }
                        }
                    } catch (_: TimeoutCancellationException) { error = "Browser sign-in expired. Please try again." }
                    catch (cancelled: CancellationException) { throw cancelled }
                    catch (failure: Exception) { error = failure.message }
                    finally { signingIn = false }
                }
            } else {
                val token = withContext(Dispatchers.IO) { client.signIn(remote.username, remote.password) }
                attach(Connection(remote.name, remote.endpoint, token, remote.id))
            }
        } catch (trust: CertificateApproval) {
            require(remote.expectedFingerprint.isEmpty() || remote.expectedFingerprint == fingerprint(certificate(trust.pem))) {
                "The certificate does not match the connection link. Verify the server address and fingerprint with its owner."
            }
            pendingTrust = remote to trust.pem
        }
        finally { if (!remote.browser || signInJob?.isActive != true) signingIn = false }
    }
    fun cancelSignIn() { signInJob?.cancel(); signingIn = false; browserURL = null }
    suspend fun approveTrust() {
        val (remote, pem) = pendingTrust ?: return
        pendingTrust = null
        val approved = remote.copy(endpoint = remote.endpoint.copy(certificatePEM = pem))
        saveRemote(approved); connectRemote(approved)
    }
    suspend fun importFile(uri: Uri) {
        if (!running && resumeLocal) { resumeLocal = false; start() }
        serverLock.withLock { /* Wait for a foreground resume already starting the server. */ }
        check(running) { "Start the on-device server to import files." }
        val file = withContext(Dispatchers.IO) {
            val resolver = context.contentResolver
            val name = resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use {
                if (it.moveToFirst()) it.getString(0) else null
            } ?: "import.csv"
            importReader(name)
            val safe = File(name).name.replace('/', '_').replace('\\', '_')
            var output = File(paths.imports, safe); var n = 2
            while (output.exists()) { output = File(paths.imports, "${safe.substringBeforeLast('.')} ${n++}.${safe.substringAfterLast('.')}") }
            try { resolver.openInputStream(uri)?.use { input -> output.outputStream().use { input.copyTo(it) } } ?: error("Cannot read this file.") }
            catch (failure: Exception) { output.delete(); throw failure }
            output
        }
        val config = runningSettings ?: settings
        val endpoint = Endpoint("${if (config.usesTLS) "https" else "http"}://127.0.0.1:$localPort",
            if (config.usesTLS) withContext(Dispatchers.IO) { paths.certificate.readText() } else "")
        val table = tableName(file.name)
        withContext(Dispatchers.IO) {
            val client = ChargewireClient(endpoint)
            val token = client.signIn("admin", vault.local().first)
            client.execute("CREATE TABLE ${sqlIdentifier(table)} AS SELECT * FROM ${importReader(file.name)}(${sqlString(file.path)})", token)
        }
        message = "Imported ${file.name} into table $table."
    }

    suspend fun localPassword(): String = withContext(Dispatchers.IO) { vault.local().first }
    suspend fun regeneratePassword() {
        val restart = running
        if (restart) stop()
        withContext(Dispatchers.IO) { vault.rotateLocal() }
        if (restart) start()
        message = "New administrator password created. Find it in Sharing details. Existing sign-ins must reconnect."
    }
    suspend fun regenerateCertificate() {
        val restart = running && (runningSettings?.usesTLS == true)
        if (restart) stop()
        withContext(Dispatchers.IO) { ensureCertificate(force = true) }
        if (restart) start()
        message = "New certificate created. Other devices must verify its new fingerprint."
    }
    private fun ensureCertificate(force: Boolean = false) {
        val names = listOf("DNS:localhost", "IP:127.0.0.1", "IP:::1") + localAddresses().map { "IP:$it" }
        val stamp = File(paths.private, "certificate-addresses")
        val text = names.joinToString(",")
        val valid = runCatching { certificate(paths.certificate.readText()).checkValidity(); true }.getOrDefault(false)
        if (force || !valid || !stamp.exists() || stamp.readText() != text) {
            NativeBridge.makeCertificate(paths.private.path, text); stamp.writeText(text)
        }
    }
    suspend fun logs(): String = withContext(Dispatchers.IO) {
        if (!paths.log.exists()) "The server has not written a log yet." else java.io.RandomAccessFile(paths.log, "r").use {
            it.seek((it.length() - 128_000).coerceAtLeast(0)); ByteArray((it.length() - it.filePointer).toInt()).let { bytes -> it.readFully(bytes); String(bytes, Charsets.UTF_8) }
        }
    }
}
