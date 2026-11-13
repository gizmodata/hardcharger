package com.gizmodata.hardcharger

import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.outlined.KeyboardArrowRight
import androidx.compose.material.icons.outlined.ContentCopy
import androidx.compose.material.icons.outlined.Check
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay

// Match the native iOS app's neutral surfaces, blue accent and grouped hierarchy.
// These are Android surfaces; no Apple font or platform-only material is embedded.
@Composable fun HardchargerTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    val blue = if (dark) Color(0xFF39A5F8) else Color(0xFF0865E8)
    val text = if (dark) Color(0xFFF5F5F7) else Color(0xFF19191C)
    val muted = if (dark) Color(0xFFA1A1A7) else Color(0xFF717178)
    val surface = if (dark) Color(0xFF1C1C1E) else Color.White
    val quiet = if (dark) Color(0xFF29292C) else Color(0xFFEAEAED)
    val colors = (if (dark) darkColorScheme() else lightColorScheme()).copy(
        primary = blue, onPrimary = Color.White, secondary = blue, onSecondary = Color.White,
        primaryContainer = quiet, onPrimaryContainer = blue,
        secondaryContainer = quiet, onSecondaryContainer = blue,
        background = if (dark) Color.Black else Color(0xFFF2F2F7), onBackground = text,
        surface = surface, onSurface = text, surfaceVariant = quiet, onSurfaceVariant = muted,
        surfaceTint = Color.Transparent, surfaceContainer = surface, surfaceContainerLow = surface,
        surfaceContainerHigh = quiet, surfaceContainerHighest = quiet,
        outline = if (dark) Color(0xFF454549) else Color(0xFFD1D1D6),
        outlineVariant = if (dark) Color(0xFF303033) else Color(0xFFE5E5EA))
    MaterialTheme(colorScheme = colors, typography = Typography(
        headlineLarge = TextStyle(fontSize = 34.sp, lineHeight = 41.sp, fontWeight = FontWeight.Bold, letterSpacing = (-0.7).sp),
        headlineMedium = TextStyle(fontSize = 28.sp, lineHeight = 34.sp, fontWeight = FontWeight.Bold),
        titleLarge = TextStyle(fontSize = 23.sp, lineHeight = 29.sp, fontWeight = FontWeight.Bold, letterSpacing = (-0.3).sp),
        titleMedium = TextStyle(fontSize = 17.sp, lineHeight = 23.sp, fontWeight = FontWeight.SemiBold),
        bodyLarge = TextStyle(fontSize = 16.sp, lineHeight = 22.sp),
        bodyMedium = TextStyle(fontSize = 15.sp, lineHeight = 21.sp),
        bodySmall = TextStyle(fontSize = 13.sp, lineHeight = 18.sp),
        labelLarge = TextStyle(fontSize = 16.sp, lineHeight = 21.sp, fontWeight = FontWeight.Medium),
        labelMedium = TextStyle(fontSize = 12.sp, lineHeight = 16.sp, fontWeight = FontWeight.Medium),
        labelSmall = TextStyle(fontSize = 11.sp, lineHeight = 15.sp, fontWeight = FontWeight.Medium)
    ), shapes = Shapes(small = RoundedCornerShape(12.dp), medium = RoundedCornerShape(20.dp),
        large = RoundedCornerShape(24.dp), extraLarge = RoundedCornerShape(30.dp)), content = content)
}

@Composable fun PageColumn(content: @Composable ColumnScope.() -> Unit) {
    Box(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background), contentAlignment = Alignment.TopCenter) {
        Column(Modifier.widthIn(max = 760.dp).fillMaxSize().verticalScroll(rememberScrollState())
            .padding(horizontal = 16.dp, vertical = 12.dp), verticalArrangement = Arrangement.spacedBy(24.dp), content = content)
    }
}
@Composable fun PageTitle(title: String) {
    Text(title, Modifier.padding(top = 4.dp, bottom = 2.dp), style = MaterialTheme.typography.headlineLarge)
}
@Composable fun Section(title: String, content: @Composable ColumnScope.() -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(9.dp)) {
        if (title.isNotEmpty()) Text(title, Modifier.padding(horizontal = 16.dp),
            style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.SemiBold), color = MaterialTheme.colorScheme.onSurfaceVariant)
        Surface(Modifier.fillMaxWidth(), shape = RoundedCornerShape(24.dp), color = MaterialTheme.colorScheme.surface) {
            Column(Modifier.padding(horizontal = 16.dp, vertical = 6.dp), content = content)
        }
    }
}
@Composable fun RowDivider() { HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant) }
@Composable fun Footnote(text: String) {
    Text(text, Modifier.padding(horizontal = 16.dp), style = MaterialTheme.typography.bodySmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant)
}
@Composable fun InfoRow(label: String, value: String) {
    if (LocalConfiguration.current.fontScale >= 1.3f) {
        Column(Modifier.fillMaxWidth().padding(vertical = 10.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(label, style = MaterialTheme.typography.bodyLarge)
            SelectionContainer { Text(value, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant) }
        }
        return
    }
    Row(Modifier.fillMaxWidth().heightIn(min = 46.dp).padding(vertical = 10.dp),
        horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(label, Modifier.weight(1f), style = MaterialTheme.typography.bodyLarge)
        SelectionContainer { Text(value, Modifier.widthIn(max = 230.dp), style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant, textAlign = TextAlign.End) }
    }
}

@Composable fun CopyButton(label: String, value: String, sensitive: Boolean = false) {
    val context = LocalContext.current
    var copied by remember { mutableStateOf(false) }
    LaunchedEffect(copied) { if (copied) { delay(2000); copied = false } }
    IconButton(enabled = value.isNotEmpty(), onClick = {
        val clip = android.content.ClipData.newPlainText(label, value)
        if (sensitive) clip.description.extras = android.os.PersistableBundle().apply {
            putBoolean("android.content.extra.IS_SENSITIVE", true)
        }
        context.getSystemService(android.content.ClipboardManager::class.java).setPrimaryClip(clip)
        copied = true
    }) {
        Icon(if (copied) Icons.Outlined.Check else Icons.Outlined.ContentCopy,
            if (copied) "Copied $label" else "Copy $label", tint = MaterialTheme.colorScheme.primary)
    }
}

@Composable fun CopyableRow(label: String, value: String) {
    Column(Modifier.fillMaxWidth().padding(vertical = 8.dp)) {
        Text(label, style = MaterialTheme.typography.bodyLarge)
        Row(verticalAlignment = Alignment.CenterVertically) {
            SelectionContainer(Modifier.weight(1f)) {
                Text(value, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            CopyButton(label, value)
        }
    }
}
@Composable fun ActionRow(label: String, icon: ImageVector, value: String = "", enabled: Boolean = true,
                         chevron: Boolean = true, action: () -> Unit) {
    Row(Modifier.fillMaxWidth().heightIn(min = 50.dp).clickable(enabled = enabled, onClick = action).padding(vertical = 12.dp),
        horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.CenterVertically) {
        val color = if (enabled) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant
        Icon(icon, null, Modifier.size(22.dp), tint = color)
        Text(label, Modifier.weight(1f), color = color, style = MaterialTheme.typography.bodyLarge)
        if (value.isNotEmpty()) Text(value, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        if (chevron) Icon(Icons.AutoMirrored.Outlined.KeyboardArrowRight, null, Modifier.size(18.dp), tint = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}
@Composable fun NumberRow(label: String, value: String, change: (String) -> Unit) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        Text(label, Modifier.weight(1f), style = MaterialTheme.typography.bodyLarge)
        TextField(value, change, Modifier.width(104.dp).semantics { contentDescription = label }, singleLine = true,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
            textStyle = MaterialTheme.typography.bodyLarge.copy(textAlign = TextAlign.End, fontFamily = FontFamily.Monospace),
            colors = TextFieldDefaults.colors(focusedContainerColor = Color.Transparent, unfocusedContainerColor = Color.Transparent,
                focusedIndicatorColor = Color.Transparent, unfocusedIndicatorColor = Color.Transparent))
    }
}
@Composable fun GlassCard(content: @Composable ColumnScope.() -> Unit) {
    val dark = isSystemInDarkTheme()
    val shape = RoundedCornerShape(28.dp)
    Column(Modifier.fillMaxWidth().border(1.dp, MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.7f), shape)
        .background(Brush.verticalGradient(if (dark) listOf(Color(0xFF19191B), Color(0xFF111113))
            else listOf(Color.White, Color(0xFFF9F9FC))), shape).padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(18.dp), content = content)
}
