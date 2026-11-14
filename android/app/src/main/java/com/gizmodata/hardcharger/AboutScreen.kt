package com.gizmodata.hardcharger

import androidx.compose.foundation.Image
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Description
import androidx.compose.material.icons.outlined.Public
import androidx.compose.material.icons.outlined.PrivacyTip
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.time.Year

@Composable fun AboutScreen(notices: () -> Unit, privacy: () -> Unit) {
    val uriHandler = LocalUriHandler.current
    val device = if (LocalConfiguration.current.smallestScreenWidthDp >= 600) "Android tablet" else "Android phone"
    PageColumn {
        Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 12.dp),
            horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(18.dp)) {
            Image(painterResource(R.drawable.about_mascot), "Hardcharger™",
                Modifier.widthIn(max = 260.dp).fillMaxWidth().aspectRatio(780f / 673f))
            Text("The Hardcharger™ server, on your $device: DuckDB, Arrow and the Chargewire protocol, with its web console.",
                style = MaterialTheme.typography.bodyLarge, textAlign = TextAlign.Center,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        VersionSection(includeApp = true)
        Section("") {
            ActionRow("Third-Party Software", Icons.Outlined.Description, action = notices)
            RowDivider()
            ActionRow("Privacy Policy", Icons.Outlined.PrivacyTip, action = privacy)
            RowDivider()
            ActionRow("gizmodata.com", Icons.Outlined.Public, chevron = false,
                action = { uriHandler.openUri("https://gizmodata.com") })
        }
        Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 12.dp),
            horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(14.dp)) {
            ByGizmoData()
            Text("© ${Year.now().value} GizmoData LLC. All rights reserved.", style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant, textAlign = TextAlign.Center)
            Text("Hardcharger™ and GizmoData™ are trademarks of GizmoData LLC.", style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant, textAlign = TextAlign.Center)
        }
    }
}

@Composable fun VersionSection(includeApp: Boolean = false) {
    val versions = remember { NativeBridge.versionDetails() }
    Section("Versions") {
        if (includeApp) {
            InfoRow("App", "${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})")
            RowDivider()
        }
        listOf(if (includeApp) "Hardcharger™ server" else "Hardcharger™", "DuckDB", "Chargewire", "Web UI")
            .forEachIndexed { index, label ->
                if (index > 0) RowDivider()
                InfoRow(label, versions[index].ifBlank { "Not bundled" })
            }
    }
}

@Composable private fun ByGizmoData() {
    Row(Modifier.clearAndSetSemantics { contentDescription = "by GizmoData™" },
        verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        Image(painterResource(R.drawable.gizmodata_mark), null, Modifier.height(30.dp).aspectRatio(142f / 153f))
        Column {
            Text("by", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Text("GizmoData™", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold,
                color = if (isSystemInDarkTheme()) Color(0xFFF6F9FF) else Color(0xFF061B49))
        }
    }
}

@Composable fun NoticesScreen() = DocumentScreen("AndroidThirdPartyNotices.txt", includeNativeNotices = true)
@Composable fun PrivacyScreen() = DocumentScreen("PrivacyPolicy.txt")

@Composable private fun DocumentScreen(asset: String, includeNativeNotices: Boolean = false) {
    val context = LocalContext.current.applicationContext
    val paragraphs by produceState<List<String>?>(initialValue = null, asset, includeNativeNotices) {
        val loaded = withContext(Dispatchers.IO) {
            val app = context.assets.open(asset).bufferedReader().use { it.readText() }
            (app + if (includeNativeNotices) "\n\n" + NativeBridge.notices() else "").split("\n\n")
        }
        // Keep the state change that creates the lazy list on Android's UI thread,
        // including when the composition's effect dispatcher resumes on an IO worker.
        withContext(Dispatchers.Main.immediate) { value = loaded }
    }
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.TopCenter) {
        if (paragraphs == null) CircularProgressIndicator(Modifier.padding(24.dp))
        else SelectionContainer {
            // Lay out the long bundled licenses lazily so opening this screen stays responsive.
            LazyColumn(Modifier.widthIn(max = 760.dp).fillMaxSize(), contentPadding = PaddingValues(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)) {
                items(paragraphs!!) { paragraph ->
                    Text(paragraph, fontFamily = if (includeNativeNotices) FontFamily.Monospace else FontFamily.Default,
                        style = if (includeNativeNotices) MaterialTheme.typography.bodySmall else MaterialTheme.typography.bodyMedium)
                }
            }
        }
    }
}
