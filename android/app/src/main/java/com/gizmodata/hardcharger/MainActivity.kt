package com.gizmodata.hardcharger

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.webkit.ValueCallback
import androidx.activity.compose.BackHandler
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.browser.customtabs.CustomTabsIntent
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material.icons.automirrored.outlined.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.*
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.input.key.*
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import com.google.zxing.BarcodeFormat
import com.google.zxing.qrcode.QRCodeWriter
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import java.io.File

class MainActivity : ComponentActivity() {
    private val model get() = (application as HardchargerApplication).model
    private var webFiles: ValueCallback<Array<Uri>>? = null
    private var pendingExport: File? = null
    private val picker = registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        model.documentPickerActive = false
        webFiles?.onReceiveValue(uris.takeIf { it.isNotEmpty() }?.toTypedArray()); webFiles = null
    }
    private val importer = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        model.documentPickerActive = false
        uri?.let { model.launch { model.importFile(it) } }
    }
    private val saver = registerForActivityResult(ActivityResultContracts.CreateDocument("application/octet-stream")) { uri ->
        model.documentPickerActive = false
        val source = pendingExport; pendingExport = null
        if (uri != null && source != null) model.launch {
            withContext(Dispatchers.IO) { contentResolver.openOutputStream(uri)?.use { output -> source.inputStream().use { it.copyTo(output) } } ?: error("Could not save this file.") }
            model.message = "Export saved."
        }
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState); enableEdgeToEdge()
        savedInstanceState?.getString("pendingExport")?.let { name -> pendingExport = File(model.paths.exports, File(name).name) }
        setContent {
            HardchargerTheme {
                AppScreen(model, { model.documentPickerActive = true; importer.launch(arrayOf("*/*")) }, { callback, types ->
                    webFiles?.onReceiveValue(null); webFiles = callback; model.documentPickerActive = true
                    // HTML accept also contains extensions; Android's picker accepts only MIME types.
                    // Let the workspace validate extensions after selection.
                    picker.launch(if (types.any { it.startsWith('.') }) arrayOf("*/*") else types)
                }, { name, bytes ->
                    model.launch {
                        val output = withContext(Dispatchers.IO) {
                            File(model.paths.exports, File(name).name.ifBlank { "export.csv" }).apply { writeBytes(bytes) }
                        }
                        pendingExport = output; model.documentPickerActive = true; saver.launch(output.name)
                    }
                }, { file -> pendingExport = file; model.documentPickerActive = true; saver.launch(file.name) })
            }
        }
        if (savedInstanceState == null) acceptShare(intent)
    }
    override fun onNewIntent(intent: Intent) { super.onNewIntent(intent); acceptShare(intent) }
    override fun onSaveInstanceState(outState: Bundle) {
        pendingExport?.let { outState.putString("pendingExport", it.name) }
        super.onSaveInstanceState(outState)
    }
    private fun acceptShare(intent: Intent) {
        if (intent.action == Intent.ACTION_VIEW && intent.data?.scheme == "hardcharger") {
            model.launch { model.pendingConnection = connectionLink(intent.data!!) }
        }
        if (intent.action == Intent.ACTION_SEND) {
            @Suppress("DEPRECATION") val uri = intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)
            uri?.let { model.launch { if (!model.running) model.start(); model.importFile(it) } }
        }
    }
    override fun onDestroy() { webFiles?.onReceiveValue(null); webFiles = null; super.onDestroy() }
}

private enum class Screen(val label: String, val icon: ImageVector) {
    Web("Web UI", Icons.Outlined.WebAsset), Console("Console", Icons.Outlined.Terminal),
    Server("Server", Icons.Outlined.Dns), Logs("Logs", Icons.AutoMirrored.Outlined.Subject),
    Servers("Saved servers", Icons.Outlined.Storage), Settings("Settings", Icons.Outlined.Settings),
    About("About", Icons.Outlined.Info), Notices("Third-Party Software", Icons.Outlined.Description),
    Privacy("Privacy Policy", Icons.Outlined.PrivacyTip)
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable fun AppScreen(model: AppModel, import: () -> Unit,
    pickFiles: (ValueCallback<Array<Uri>>, Array<String>) -> Unit,
    export: (String, ByteArray) -> Unit, saveFile: (File) -> Unit) {
    var screen by rememberSaveable { mutableStateOf(Screen.Server) }
    var confirm by remember { mutableStateOf<String?>(null) }
    var sharing by remember { mutableStateOf(false) }
    val tablet = LocalConfiguration.current.screenWidthDp >= 720
    val context = LocalContext.current
    LaunchedEffect(model.pendingConnection) { if (model.pendingConnection != null) screen = Screen.Servers }
    LaunchedEffect(model.browserURL) {
        model.browserURL?.let { url -> CustomTabsIntent.Builder().build().launchUrl(context, Uri.parse(url)); model.browserURL = null }
    }
    LaunchedEffect(model.running, model.settings.share, model.settings.keepAwake) {
        val activity = context as? android.app.Activity
        if (model.running && model.settings.share && model.settings.keepAwake) activity?.window?.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        else activity?.window?.clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
    }
    val primaryScreens = listOf(Screen.Web, Screen.Console, Screen.Server, Screen.Settings)
    val selectedTab = when (screen) {
        Screen.Logs -> Screen.Server
        Screen.Servers, Screen.About, Screen.Notices, Screen.Privacy -> Screen.Settings
        else -> screen
    }
    val parent = when (screen) {
        Screen.Logs -> Screen.Server
        Screen.Servers, Screen.About -> Screen.Settings
        Screen.Notices, Screen.Privacy -> Screen.About
        else -> null
    }
    BackHandler(enabled = parent != null) { parent?.let { screen = it } }
    Scaffold(containerColor = MaterialTheme.colorScheme.background, topBar = {
        if (screen !in listOf(Screen.Server, Screen.Settings)) CenterAlignedTopAppBar(
            title = { Text(screen.label, style = MaterialTheme.typography.titleMedium) },
            colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.background),
            navigationIcon = {
                if (parent != null) IconButton(onClick = { screen = parent }) {
                    Icon(Icons.AutoMirrored.Outlined.ArrowBack, "Back", tint = MaterialTheme.colorScheme.primary)
                }
            }, actions = {
                if (model.signingIn) TextButton(onClick = { model.cancelSignIn() }) { Text("Cancel sign-in") }
                if (model.busy || model.signingIn) CircularProgressIndicator(Modifier.padding(12.dp).size(20.dp), strokeWidth = 2.dp)
            })
    }, bottomBar = {
        if (!tablet) Box(Modifier.fillMaxWidth().navigationBarsPadding().padding(horizontal = 16.dp, vertical = 8.dp), contentAlignment = Alignment.Center) {
            Surface(Modifier.widthIn(max = 480.dp).fillMaxWidth(), shape = RoundedCornerShape(40.dp),
                color = MaterialTheme.colorScheme.surface.copy(alpha = 0.96f), shadowElevation = 8.dp,
                border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.65f))) {
                Row(Modifier.padding(5.dp), horizontalArrangement = Arrangement.spacedBy(3.dp)) {
                    primaryScreens.forEach { item ->
                        val selected = selectedTab == item
                        Column(Modifier.weight(1f).clip(RoundedCornerShape(32.dp))
                            .background(if (selected) MaterialTheme.colorScheme.surfaceVariant else Color.Transparent)
                            .selectable(selected, role = Role.Tab, onClick = { screen = item })
                            .semantics { contentDescription = item.label }
                            .padding(vertical = 8.dp), horizontalAlignment = Alignment.CenterHorizontally,
                            verticalArrangement = Arrangement.spacedBy(3.dp)) {
                            val tint = if (selected) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurface
                            Icon(item.icon, null, Modifier.size(24.dp), tint = tint)
                            Text(item.label, color = tint, style = MaterialTheme.typography.labelSmall, maxLines = 1)
                        }
                    }
                }
            }
        }
    }) { padding ->
        Row(Modifier.fillMaxSize().padding(padding)) {
            if (tablet) NavigationRail(containerColor = MaterialTheme.colorScheme.background) {
                Image(painterResource(R.drawable.head_mark), null, Modifier.padding(vertical = 18.dp).size(42.dp))
                listOf(Screen.Web, Screen.Console, Screen.Server, Screen.Logs, Screen.Servers, Screen.Settings).forEach { item ->
                    NavigationRailItem(selected = (if (screen in listOf(Screen.About, Screen.Notices, Screen.Privacy)) Screen.Settings else screen) == item,
                    onClick = { screen = item }, icon = { Icon(item.icon, item.label) }, label = { Text(item.label) }) }
            }
            Box(Modifier.weight(1f).fillMaxHeight()) {
                Workspace(model, pickFiles, export, visible = screen == Screen.Web)
                when (screen) {
                    Screen.Web -> Unit
                    Screen.Console -> ConsoleScreen(model)
                    Screen.Server -> ServerScreen(model, import, { confirm = it }, { sharing = true }, { screen = Screen.Web }, saveFile, { screen = Screen.Logs })
                    Screen.Logs -> LogsScreen(model)
                    Screen.Servers -> RemoteServersScreen(model) { screen = Screen.Web }
                    Screen.Settings -> SettingsScreen(model, { confirm = "restart" }, { screen = Screen.Servers }, { sharing = true }, { screen = Screen.About })
                    Screen.About -> AboutScreen({ screen = Screen.Notices }, { screen = Screen.Privacy })
                    Screen.Notices -> NoticesScreen()
                    Screen.Privacy -> PrivacyScreen()
                }
            }
        }
    }
    confirm?.let { action -> AlertDialog(onDismissRequest = { confirm = null },
        title = { Text(if (action == "stop") "Stop this server?" else "Restart this server?") },
        text = { Text("Active queries and connections to this device will close. Your database stays saved.") },
        dismissButton = { TextButton(onClick = { confirm = null }) { Text("Cancel") } },
        confirmButton = { TextButton(onClick = { confirm = null; model.launch { if (action == "stop") model.stop() else model.restart() } }) { Text(if (action == "stop") "Stop" else "Restart") } }) }
    model.error?.let { text -> AlertDialog(onDismissRequest = { model.error = null }, title = { Text("Could not complete the action") },
        text = { SelectionContainer { Text(text) } }, confirmButton = { TextButton(onClick = { model.error = null }) { Text("OK") } }) }
    model.message?.let { text -> AlertDialog(onDismissRequest = { model.message = null }, title = { Text("Hardcharger") },
        text = { Text(text) }, confirmButton = { TextButton(onClick = { model.message = null }) { Text("OK") } }) }
    model.pendingTrust?.let { (remote, pem) -> AlertDialog(onDismissRequest = { model.pendingTrust = null },
        title = { Text("Trust this server certificate?") }, text = { Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("Verify this SHA-256 fingerprint with the administrator of ${remote.name} before continuing.")
            SelectionContainer { Text(fingerprint(certificate(pem)), fontFamily = FontFamily.Monospace) }
            Text(remote.endpoint.origin, Modifier.padding(top = 12.dp, bottom = 8.dp))
        } }, dismissButton = { TextButton(onClick = { model.pendingTrust = null }) { Text("Cancel") } },
        confirmButton = { TextButton(onClick = { model.launch { model.approveTrust() } }) { Text("Trust certificate") } }) }
    if (sharing) ShareDialog(model) { sharing = false }
}

@Composable fun EmptyPanel(title: String, text: String) {
    Column(Modifier.fillMaxSize().padding(32.dp), verticalArrangement = Arrangement.Center, horizontalAlignment = Alignment.CenterHorizontally) {
        Image(painterResource(R.drawable.head_mark), null, Modifier.size(80.dp))
        Spacer(Modifier.height(24.dp)); Text(title, style = MaterialTheme.typography.headlineSmall)
        Spacer(Modifier.height(12.dp)); Text(text, style = MaterialTheme.typography.bodyLarge)
    }
}
@Composable private fun ServerScreen(model: AppModel, import: () -> Unit, confirm: (String) -> Unit,
                                    share: () -> Unit, workspace: () -> Unit, saveFile: (File) -> Unit, logs: () -> Unit) {
    var now by remember { mutableStateOf(System.currentTimeMillis()) }
    LaunchedEffect(model.running) { while (true) { now = System.currentTimeMillis(); delay(1000) } }
    val current = model.runningSettings ?: model.settings
    val tablet = LocalConfiguration.current.screenWidthDp >= 720
    PageColumn {
        PageTitle("Server")
        GlassCard {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                Image(painterResource(R.drawable.head_mark), null, Modifier.size(64.dp))
                Column(verticalArrangement = Arrangement.spacedBy(5.dp)) {
                    Text("Hardcharger™", style = MaterialTheme.typography.titleLarge)
                    Text(if (tablet) "on this tablet" else "on this phone", color = MaterialTheme.colorScheme.onSurfaceVariant)
                    val status = if (model.busy) "Updating…" else if (model.running) "Running" else "Stopped"
                    val color = if (model.busy) Color(0xFFB87912) else if (model.running) Color(0xFF187E3B) else MaterialTheme.colorScheme.onSurfaceVariant
                    Row(Modifier.background(color.copy(alpha = 0.14f), RoundedCornerShape(30.dp)).padding(horizontal = 11.dp, vertical = 5.dp)
                        .semantics(mergeDescendants = true) { contentDescription = "Server status: $status" },
                        verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        Box(Modifier.size(7.dp).background(color, RoundedCornerShape(50)))
                        Text(status, color = if (isSystemInDarkTheme() && model.running) Color(0xFF65DA8C) else color,
                            style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.SemiBold))
                    }
                }
            }
            if (model.running) Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                HeroMetric("Port", model.localPort.toString(), Modifier.weight(1f))
                VerticalDivider(Modifier.height(30.dp), color = MaterialTheme.colorScheme.outlineVariant)
                val seconds = ((now - model.startedAt) / 1000).coerceAtLeast(0)
                HeroMetric("Up", if (seconds < 60) "${seconds}s" else "${seconds / 60}m ${seconds % 60}s", Modifier.weight(1f))
                VerticalDivider(Modifier.height(30.dp), color = MaterialTheme.colorScheme.outlineVariant)
                HeroMetric("TLS", if (current.usesTLS) "On" else "Off", Modifier.weight(1f))
            }
            ServerActions(model, confirm, workspace)
        }
        Section("Address") {
            if (model.running) CopyableRow("On this device", "${if (current.usesTLS) "https" else "http"}://127.0.0.1:${model.localPort}/ui/")
            else InfoRow("Port", current.port.toString())
            RowDivider()
            ActionRow("Share on Local Network", Icons.Outlined.Wifi, if (current.share && model.running) "On" else "Off", action = share)
        }
        Section("Database") {
            InfoRow("File", "Hardcharger.duckdb"); RowDivider()
            InfoRow("Size", "%.1f MB".format(model.paths.database.length() / 1048576.0)); RowDivider()
            ActionRow("Import CSV, JSON or Parquet…", Icons.Outlined.FileDownload, enabled = model.running && !model.busy, action = import)
        }
        Footnote("Import files from your device. For Excel workbooks, use Upload in the web workspace.")
        Section("Resources") {
            InfoRow("Memory limit", "${current.resolvedMemoryMB(model.paths.physicalMemoryBytes)} MB"); RowDivider()
            InfoRow("Threads", if (current.threads == 0) "One per core (${model.paths.processorCount})" else current.threads.toString())
        }
        Section("") { ActionRow("Server Log", Icons.AutoMirrored.Outlined.Subject, action = logs) }
        Section("Exports") {
            Text("Write SQL exports to this folder, then save a copy to your device.", Modifier.padding(vertical = 10.dp),
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            SelectionContainer { Text(model.paths.exports.path, Modifier.padding(bottom = 10.dp),
                style = MaterialTheme.typography.bodySmall, fontFamily = FontFamily.Monospace) }
            model.paths.exports.listFiles()?.filter { it.isFile }?.takeLast(10)?.forEach { file ->
                RowDivider(); ActionRow("Save ${file.name}…", Icons.Outlined.FileDownload, action = { saveFile(file) })
            }
        }
        VersionSection()
    }
}
@Composable private fun ServerActions(model: AppModel, confirm: (String) -> Unit, workspace: () -> Unit) {
    val toggle: @Composable (Modifier) -> Unit = { modifier ->
        Button(onClick = { if (model.running) confirm("stop") else model.launch { model.start() } },
            enabled = !model.busy, modifier = modifier.heightIn(min = 48.dp)) {
            Icon(if (model.running) Icons.Filled.Stop else Icons.Outlined.PlayArrow, null, Modifier.size(18.dp))
            Spacer(Modifier.width(9.dp)); Text(if (model.running) "Stop" else "Start")
        }
    }
    val restart: @Composable (Modifier) -> Unit = { modifier ->
        FilledTonalButton(onClick = { confirm("restart") }, enabled = !model.busy, modifier = modifier.heightIn(min = 48.dp),
            colors = ButtonDefaults.filledTonalButtonColors(contentColor = MaterialTheme.colorScheme.onSurface)) {
            Icon(Icons.Outlined.Refresh, null, Modifier.size(21.dp)); Spacer(Modifier.width(7.dp)); Text("Restart")
        }
    }
    val web: @Composable (Modifier) -> Unit = { modifier ->
        FilledTonalButton(onClick = workspace, modifier = modifier.heightIn(min = 48.dp),
            colors = ButtonDefaults.filledTonalButtonColors(contentColor = MaterialTheme.colorScheme.onSurface)) {
            Icon(Icons.Outlined.WebAsset, null, Modifier.size(21.dp)); Spacer(Modifier.width(7.dp)); Text("Web UI")
        }
    }
    BoxWithConstraints {
        if (maxWidth >= 480.dp && model.running) Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            toggle(Modifier.weight(1f)); restart(Modifier.weight(1f)); web(Modifier.weight(1f))
        } else Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            toggle(Modifier.fillMaxWidth())
            if (model.running) Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                restart(Modifier.weight(1f)); web(Modifier.weight(1f))
            }
        }
    }
}
@Composable private fun HeroMetric(label: String, value: String, modifier: Modifier) {
    Column(modifier, horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Text(value, style = MaterialTheme.typography.titleMedium, maxLines = 1)
        Text(label, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable private fun ConsoleScreen(model: AppModel) {
    var input by rememberSaveable { mutableStateOf("") }
    var historyIndex by remember { mutableStateOf(-1) }
    val scroll = rememberScrollState()
    val focus = LocalFocusManager.current
    LaunchedEffect(model.transcript) { scroll.animateScrollTo(scroll.maxValue) }
    fun run() { val sql = input; input = ""; historyIndex = -1; model.launch { model.run(sql) } }
    if (model.connection == null) { EmptyPanel("SQL console", "Start or connect to a server to run SQL and console commands."); return }
    Column(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background).imePadding().padding(horizontal = 14.dp, vertical = 4.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(model.connection!!.label, Modifier.weight(1f), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            IconButton(onClick = { model.clearConsole() }) { Icon(Icons.Outlined.DeleteOutline, "Clear console") }
        }
        Surface(Modifier.weight(1f).fillMaxWidth(), color = MaterialTheme.colorScheme.background) {
            val prompt = MaterialTheme.colorScheme.primary
            val transcript = remember(model.transcript, prompt) { buildAnnotatedString {
                model.transcript.lineSequence().forEach { line ->
                    if (line.startsWith("›")) withStyle(SpanStyle(color = prompt)) { append(line) } else append(line)
                    append('\n')
                }
            } }
            SelectionContainer { Text(transcript, Modifier.verticalScroll(scroll).horizontalScroll(rememberScrollState()).padding(vertical = 10.dp),
                fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall) }
        }
        TextField(value = input, onValueChange = { value ->
            if (value.endsWith('\n') && !input.endsWith('\n') && NativeBridge.complete(value)) {
                input = value.trimEnd(); run()
            } else input = value
        }, label = { Text("SQL or .help") }, modifier = Modifier.fillMaxWidth().heightIn(min = 64.dp, max = 160.dp)
            .onPreviewKeyEvent { event ->
                if (event.type == KeyEventType.KeyDown && event.key == Key.Enter && (event.isCtrlPressed || event.isMetaPressed)) { run(); true }
                else if (event.type == KeyEventType.KeyDown && event.key == Key.Escape) { if (model.querying) model.cancel() else input = ""; true } else false
            }, textStyle = MaterialTheme.typography.bodyMedium.copy(fontFamily = FontFamily.Monospace, color = MaterialTheme.colorScheme.primary), enabled = !model.querying,
            shape = RoundedCornerShape(16.dp), colors = TextFieldDefaults.colors(focusedContainerColor = MaterialTheme.colorScheme.surface,
                unfocusedContainerColor = MaterialTheme.colorScheme.surface, unfocusedIndicatorColor = Color.Transparent, focusedIndicatorColor = Color.Transparent))
        Text("; completes SQL · Ctrl+Enter runs", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            IconButton(enabled = model.history.isNotEmpty() && !model.querying, onClick = {
                historyIndex = if (historyIndex < 0) model.history.lastIndex else (historyIndex - 1).coerceAtLeast(0)
                input = model.history[historyIndex]
            }) { Icon(Icons.Outlined.KeyboardArrowUp, "Previous command") }
            IconButton(enabled = historyIndex >= 0 && !model.querying, onClick = {
                if (historyIndex < model.history.lastIndex) { historyIndex++; input = model.history[historyIndex] }
                else { historyIndex = -1; input = "" }
            }) { Icon(Icons.Outlined.KeyboardArrowDown, "Next command") }
            IconButton(enabled = input.isNotEmpty() && !model.querying, onClick = { input = ""; historyIndex = -1 }) {
                Icon(Icons.Outlined.Cancel, "Clear input")
            }
            if (WindowInsets.isImeVisible) IconButton(onClick = { focus.clearFocus() }) { Icon(Icons.Outlined.KeyboardHide, "Hide keyboard") }
            Spacer(Modifier.weight(1f))
            if (model.querying) OutlinedButton(onClick = { model.cancel() }) { Text("Cancel") }
            else Button(onClick = { run() }, enabled = input.isNotBlank()) { Text("Run") }
        }
    }
}

@Composable private fun SettingsScreen(model: AppModel, restart: () -> Unit, servers: () -> Unit, share: () -> Unit, about: () -> Unit) {
    var port by rememberSaveable { mutableStateOf(model.settings.port.toString()) }
    var memory by rememberSaveable { mutableStateOf(model.settings.memoryMB.toString()) }
    var threads by rememberSaveable { mutableIntStateOf(model.settings.threads) }
    var auto by rememberSaveable { mutableStateOf(model.settings.autoStart) }
    var tls by rememberSaveable { mutableStateOf(model.settings.tls) }
    var awake by rememberSaveable { mutableStateOf(model.settings.keepAwake) }
    var queryLogs by rememberSaveable { mutableStateOf(model.settings.printQueries) }
    var logLevel by rememberSaveable { mutableStateOf(model.settings.logLevel) }
    var regenerate by remember { mutableStateOf<String?>(null) }
    PageColumn {
        PageTitle("Settings")
        Section("Server") {
            ToggleRow("Start when the app opens", auto) { auto = it }; RowDivider()
            NumberRow("Port", port) { port = it }; RowDivider()
            MemoryLimitPicker(memory) { memory = it }; RowDivider()
            ThreadsPicker(threads, model.paths.processorCount) { threads = it }
        }
        Footnote("Automatic is ${ServerSettings().resolvedMemoryMB(model.paths.physicalMemoryBytes)} MB on this device. Larger work spills to device storage. One per core uses ${model.paths.processorCount} threads.")
        Footnote("The local server pauses after 30 seconds away and resumes when you return. Document pickers receive extra time.")
        Section("Logging") {
            Row(Modifier.fillMaxWidth().padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                Text("Log level", Modifier.weight(1f))
                var chooseLevel by remember { mutableStateOf(false) }
                Box {
                    TextButton(onClick = { chooseLevel = true }) { Text(logLevel.replaceFirstChar { it.uppercase() }); Icon(Icons.Outlined.UnfoldMore, null, Modifier.size(18.dp)) }
                    DropdownMenu(chooseLevel, { chooseLevel = false }) {
                        listOf("info", "debug", "warning", "error").forEach { level -> DropdownMenuItem(
                            text = { Text(level.replaceFirstChar { it.uppercase() }) }, onClick = { logLevel = level; chooseLevel = false }) }
                    }
                }
            }
            RowDivider(); ToggleRow("Log every statement", queryLogs) { queryLogs = it }
            RowDivider()
            Row(Modifier.fillMaxWidth().padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                Text("Log view", Modifier.weight(1f))
                var chooseStyle by remember { mutableStateOf(false) }
                Box {
                    TextButton(onClick = { chooseStyle = true }) {
                        Text(model.logStyle.label); Icon(Icons.Outlined.UnfoldMore, null, Modifier.size(18.dp))
                    }
                    DropdownMenu(chooseStyle, { chooseStyle = false }) {
                        LogStyle.entries.forEach { style -> DropdownMenuItem(text = { Text(style.label) },
                            onClick = { model.updateLogStyle(style); chooseStyle = false }) }
                    }
                }
            }
        }
        Section("Network") {
            ActionRow("Share on Local Network", Icons.Outlined.Wifi, if (model.settings.share) "On" else "Off", action = share)
            RowDivider(); ToggleRow("Use TLS on this device too", tls) { tls = it }
            RowDivider(); ToggleRow("Keep screen on while sharing", awake) { awake = it }
        }
        Footnote("Sharing always uses TLS, with a certificate made on this device.")
        Button(onClick = { model.launch {
            model.saveSettings(model.settings.copy(port = port.toIntOrNull() ?: 0, memoryMB = memory.toIntOrNull() ?: -1,
                threads = threads, autoStart = auto, tls = tls, keepAwake = awake, printQueries = queryLogs, logLevel = logLevel))
            if (model.running) restart() else model.message = "Settings saved."
        } }, modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)) { Text("Save settings") }
        Section("Administrator") {
            InfoRow("Username", "admin"); RowDivider()
            ActionRow("Password & sharing details", Icons.Outlined.Key, action = share); RowDivider()
            ActionRow("New password…", Icons.Outlined.LockReset, enabled = !model.busy, action = { regenerate = "password" }); RowDivider()
            ActionRow("New TLS certificate…", Icons.Outlined.VerifiedUser, enabled = !model.busy, action = { regenerate = "certificate" })
        }
        Footnote("Made on this device and protected by Android Keystore. The console signs in automatically.")
        Section("") { ActionRow("Saved Servers", Icons.Outlined.Storage, action = servers) }
        Section("") { ActionRow("About Hardcharger™", Icons.Outlined.Info, action = about) }
    }
    regenerate?.let { action -> AlertDialog(onDismissRequest = { regenerate = null },
        title = { Text("Create a new $action?") },
        text = { Text(if (action == "password") "The server restarts and existing sign-ins must reconnect with the new password."
            else "The TLS server restarts. Other devices must verify and approve the new certificate fingerprint.") },
        dismissButton = { TextButton(onClick = { regenerate = null }) { Text("Cancel") } },
        confirmButton = { TextButton(onClick = { regenerate = null; model.launch {
            if (action == "password") model.regeneratePassword() else model.regenerateCertificate()
        } }) { Text("Create new $action") } }) }
}
@Composable private fun ToggleRow(label: String, value: Boolean, change: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth().heightIn(min = 50.dp).padding(vertical = 3.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(label, Modifier.weight(1f).padding(end = 10.dp), style = MaterialTheme.typography.bodyLarge)
        Switch(value, change, colors = SwitchDefaults.colors(checkedTrackColor = Color(0xFF2BC85B), checkedThumbColor = Color.White,
            uncheckedTrackColor = MaterialTheme.colorScheme.surfaceVariant, uncheckedBorderColor = Color.Transparent))
    }
}

@Composable private fun RemoteServersScreen(model: AppModel, opened: () -> Unit) {
    var edit by remember { mutableStateOf<RemoteServer?>(null) }
    var adding by remember { mutableStateOf(false) }
    var deleting by remember { mutableStateOf<RemoteServer?>(null) }
    LaunchedEffect(model.pendingConnection) {
        model.pendingConnection?.let { edit = it; adding = true; model.pendingConnection = null }
    }
    PageColumn {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Your servers", Modifier.weight(1f), style = MaterialTheme.typography.headlineMedium)
            FilledTonalIconButton(onClick = { edit = null; adding = true }) { Icon(Icons.Outlined.Add, "Add server") }
        }
        Footnote("Your connections and passwords are protected by Android Keystore.")
        if (model.running) Section("This device") {
            Text("Your local Hardcharger database", Modifier.padding(top = 12.dp, bottom = 8.dp))
            Button(onClick = { model.launch { model.connectLocal(); opened() } }) { Text("Connect") }
        }
        if (model.remotes.isEmpty()) Text("Add a server to connect to its workspace and SQL console.", color = MaterialTheme.colorScheme.onSurfaceVariant)
        model.remotes.forEach { remote -> Section(remote.name) {
            Text(remote.endpoint.origin, Modifier.padding(top = 12.dp, bottom = 8.dp))
            Text(if (remote.browser) "Browser sign-in" else remote.username, style = MaterialTheme.typography.bodySmall)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = { model.launch { model.connectRemote(remote); if (model.connection?.remoteID == remote.id) opened() } }, enabled = !model.signingIn) { Text("Connect") }
                TextButton(onClick = { edit = remote; adding = true }) { Text("Edit") }
                IconButton(onClick = { deleting = remote }) { Icon(Icons.Outlined.DeleteOutline, "Delete ${remote.name}") }
            }
        } }
    }
    if (adding) RemoteEditor(edit, { adding = false }) { remote -> model.launch { model.saveRemote(remote); adding = false } }
    deleting?.let { remote -> AlertDialog(onDismissRequest = { deleting = null }, title = { Text("Delete ${remote.name}?") },
        text = { Text("This removes the saved connection and credentials from this device.") },
        dismissButton = { TextButton(onClick = { deleting = null }) { Text("Cancel") } },
        confirmButton = { TextButton(onClick = { deleting = null; model.launch { model.deleteRemote(remote) } }) { Text("Delete") } }) }
}
@Composable private fun RemoteEditor(existing: RemoteServer?, dismiss: () -> Unit, save: (RemoteServer) -> Unit) {
    var name by rememberSaveable { mutableStateOf(existing?.name.orEmpty()) }
    var address by rememberSaveable { mutableStateOf(existing?.endpoint?.origin.orEmpty()) }
    var username by rememberSaveable { mutableStateOf(existing?.username.orEmpty()) }
    // Password is deliberately excluded from saved instance state.
    var password by remember { mutableStateOf(existing?.password.orEmpty()) }
    var browser by rememberSaveable { mutableStateOf(existing?.browser ?: false) }
    var error by remember { mutableStateOf<String?>(null) }
    var resetTrust by remember { mutableStateOf(false) }
    AlertDialog(onDismissRequest = dismiss, title = { Text(if (existing == null) "Add server" else "Edit server") }, text = {
        Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            OutlinedTextField(name, { name = it }, label = { Text("Name") }, singleLine = true)
            OutlinedTextField(address, { address = it }, label = { Text("Server URL, including port") }, placeholder = { Text("https://server.example:31337") }, singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri))
            if (address.startsWith("http://")) Text("HTTP sends your credentials and queries without encryption. Use only on a trusted private network.", color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
            ToggleRow("Sign in with a browser", browser) { browser = it }
            if (!browser) {
                OutlinedTextField(username, { username = it }, label = { Text("Username") }, singleLine = true)
                OutlinedTextField(password, { password = it }, label = { Text("Password") }, visualTransformation = PasswordVisualTransformation(), singleLine = true)
            }
            if (!existing?.endpoint?.certificatePEM.isNullOrEmpty()) {
                Text("Certificate fingerprint", style = MaterialTheme.typography.labelMedium)
                Text(existing!!.endpoint.fingerprint, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
                ToggleRow("Forget trusted certificate", resetTrust) { resetTrust = it }
            }
            if (!existing?.expectedFingerprint.isNullOrEmpty()) {
                Text("Fingerprint from connection link", style = MaterialTheme.typography.labelMedium)
                Text(existing!!.expectedFingerprint, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
                Text("The server must present this certificate. Review the address before saving.", style = MaterialTheme.typography.bodySmall)
            }
            error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        }
    }, dismissButton = { TextButton(onClick = dismiss) { Text("Cancel") } }, confirmButton = {
        TextButton(onClick = {
            runCatching {
                require(name.isNotBlank()) { "Enter a name." }
                val parsed = Endpoint.parse(address)
                val endpoint = if (!resetTrust && existing?.endpoint?.origin == parsed.origin) parsed.copy(certificatePEM = existing.endpoint.certificatePEM) else parsed
                val pin = if (!resetTrust && existing?.endpoint?.origin == parsed.origin) existing.expectedFingerprint else ""
                save(RemoteServer(existing?.id ?: java.util.UUID.randomUUID().toString(), name.trim(), endpoint, username, if (browser) "" else password, browser, pin))
            }.onFailure { error = it.message }
        }) { Text("Save") }
    })
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable private fun ShareDialog(model: AppModel, dismiss: () -> Unit) {
    val context = LocalContext.current
    var password by remember { mutableStateOf("") }
    var showPassword by remember { mutableStateOf(false) }
    var fingerprint by remember { mutableStateOf("") }
    val addresses = remember { localAddresses() }
    val emulator = android.os.Build.FINGERPRINT.contains("generic") || android.os.Build.MODEL.contains("sdk") || android.os.Build.PRODUCT.contains("sdk")
    LaunchedEffect(model.running, model.settings.share) {
        password = model.localPassword()
        fingerprint = withContext(Dispatchers.IO) { runCatching { fingerprint(certificate(model.paths.certificate.readText())) }.getOrDefault("") }
    }
    val address = if (emulator) "https://127.0.0.1:31437/ui/" else "https://${addresses.firstOrNull() ?: "127.0.0.1"}:${model.localPort}/ui/"
    ModalBottomSheet(onDismissRequest = dismiss, sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true),
        containerColor = MaterialTheme.colorScheme.background, shape = RoundedCornerShape(topStart = 32.dp, topEnd = 32.dp)) {
        Column(Modifier.fillMaxWidth().heightIn(max = (LocalConfiguration.current.screenHeightDp * 0.85f).dp)
            .verticalScroll(rememberScrollState()).padding(horizontal = 20.dp).padding(bottom = 28.dp),
            verticalArrangement = Arrangement.spacedBy(22.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Share on Local Network", Modifier.weight(1f), style = MaterialTheme.typography.titleMedium)
                TextButton(onClick = dismiss) { Text("Done") }
            }
            Section("") {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Outlined.Wifi, null, tint = MaterialTheme.colorScheme.primary, modifier = Modifier.padding(end = 10.dp).size(22.dp))
                    Text("Share on Local Network", Modifier.weight(1f), style = MaterialTheme.typography.bodyLarge)
                    Switch(model.settings.share, enabled = !model.busy, onCheckedChange = { enabled -> model.launch {
                        model.saveSettings(model.settings.copy(share = enabled))
                        if (model.running) model.restart() else model.start()
                    } }, colors = SwitchDefaults.colors(checkedTrackColor = Color(0xFF2BC85B), checkedThumbColor = Color.White))
                }
            }
            Footnote("Anyone on this network with the administrator password can read and change your tables. Turn sharing on only on networks you trust, and keep Hardcharger open.")
            if (model.settings.share && model.running) {
                Section(if (emulator) "Open on this Mac" else "Open on a computer on this network") {
                    Column(Modifier.fillMaxWidth().padding(vertical = 18.dp), horizontalAlignment = Alignment.CenterHorizontally,
                        verticalArrangement = Arrangement.spacedBy(16.dp)) {
                        if (!emulator) QRCode(address)
                        SelectionContainer { Text(address, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodyLarge) }
                        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                            CopyButton("address", address)
                            TextButton(onClick = {
                                context.startActivity(Intent.createChooser(Intent(Intent.ACTION_SEND).apply {
                                    type = "text/plain"; putExtra(Intent.EXTRA_TEXT, address)
                                }, "Share server address"))
                            }) { Icon(Icons.Outlined.Share, null, Modifier.size(18.dp)); Spacer(Modifier.width(8.dp)); Text("Share") }
                        }
                    }
                }
                if (emulator) Footnote("The launcher forwards Mac port 31437 to this emulator. Other Wi-Fi devices cannot use this virtual-network address.")
                if (!emulator) Section("Open in Hardcharger on another device") {
                    Box(Modifier.fillMaxWidth().padding(18.dp), contentAlignment = Alignment.Center) {
                        QRCode(Uri.Builder().scheme("hardcharger").authority("connect")
                            .appendQueryParameter("url", address.removeSuffix("/ui/"))
                            .appendQueryParameter("fingerprint", fingerprint).appendQueryParameter("name", "Hardcharger on Android")
                            .appendQueryParameter("username", "admin").build().toString())
                    }
                }
            }
            Section("Administrator") {
                InfoRow("Username", "admin"); RowDivider()
                Row(Modifier.fillMaxWidth().padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Text("Password", style = MaterialTheme.typography.bodyLarge)
                        SelectionContainer { Text(if (showPassword) password else "••••••••••••", fontFamily = FontFamily.Monospace,
                            style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant) }
                    }
                    IconButton(onClick = { showPassword = !showPassword }) {
                        Icon(if (showPassword) Icons.Outlined.VisibilityOff else Icons.Outlined.Visibility,
                            if (showPassword) "Hide password" else "Show password", tint = MaterialTheme.colorScheme.primary)
                    }
                    CopyButton("password", password, sensitive = true)
                }
            }
            if (model.settings.share && model.running) {
                Section("Certificate fingerprint (SHA-256)") {
                    SelectionContainer { Text(fingerprint, Modifier.padding(vertical = 14.dp), fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall) }
                }
                Footnote("Verify this fingerprint before trusting the certificate on another device. QR codes never include your password.")
            }
        }
    }
}
@Composable private fun QRCode(text: String) {
    val bitmap = remember(text) {
        val matrix = QRCodeWriter().encode(text, BarcodeFormat.QR_CODE, 360, 360)
        android.graphics.Bitmap.createBitmap(360, 360, android.graphics.Bitmap.Config.ARGB_8888).apply {
            for (y in 0 until 360) for (x in 0 until 360) setPixel(x, y, if (matrix[x, y]) android.graphics.Color.BLACK else android.graphics.Color.WHITE)
        }
    }
    Image(bitmap.asImageBitmap(), contentDescription = "QR code for $text",
        Modifier.size(220.dp).background(Color.White, RoundedCornerShape(12.dp)).padding(10.dp))
}
