package com.gizmodata.hardcharger

import androidx.compose.foundation.clickable
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Search
import androidx.compose.material.icons.outlined.Refresh
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay

@Composable fun LogsScreen(model: AppModel) {
    var logs by remember { mutableStateOf("") }
    var follow by rememberSaveable { mutableStateOf(true) }
    var search by rememberSaveable { mutableStateOf("") }
    val state = rememberLazyListState()
    val lines = remember(logs, search) { LogLine.parse(logs).filter { it.raw.contains(search, ignoreCase = true) } }
    LaunchedEffect(Unit) { while (true) { logs = model.logs(); delay(2000) } }
    LaunchedEffect(lines, follow) { if (follow && lines.isNotEmpty()) state.scrollToItem(lines.lastIndex) }
    Column(Modifier.fillMaxSize().padding(horizontal = 16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            LogStyle.entries.forEach { style ->
                FilterChip(selected = model.logStyle == style, onClick = { model.updateLogStyle(style) }, label = { Text(style.label) })
            }
            Spacer(Modifier.weight(1f))
            IconButton(onClick = { model.launch { logs = model.logs() } }) { Icon(Icons.Outlined.Refresh, "Refresh log") }
        }
        OutlinedTextField(search, { search = it }, Modifier.fillMaxWidth(), singleLine = true,
            label = { Text("Filter") }, leadingIcon = { Icon(Icons.Outlined.Search, null) }, shape = MaterialTheme.shapes.medium)
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text("Follow New Lines", Modifier.weight(1f), style = MaterialTheme.typography.bodyMedium)
            Switch(follow, { follow = it }, colors = SwitchDefaults.colors(checkedTrackColor = Color(0xFF2BC85B), checkedThumbColor = Color.White))
        }
        if (lines.isEmpty() || logs.isBlank()) Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Text(if (search.isEmpty()) "No log yet" else "No matching lines", color = MaterialTheme.colorScheme.onSurfaceVariant)
        } else SelectionContainer {
            LazyColumn(Modifier.fillMaxSize(), state = state, contentPadding = PaddingValues(bottom = 16.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp)) {
                itemsIndexed(lines) { _, line -> LogEntry(line, model.logStyle == LogStyle.Emoji) }
            }
        }
    }
}

@Composable private fun LogEntry(line: LogLine, marks: Boolean) {
    var expanded by remember(line.raw) { mutableStateOf(false) }
    val dark = isSystemInDarkTheme()
    val color = when {
        line.level == "error" -> MaterialTheme.colorScheme.error
        line.level == "warning" -> if (dark) Color(0xFFFFC069) else Color(0xFF925500)
        line.success -> if (dark) Color(0xFF65DA8C) else Color(0xFF187E3B)
        else -> MaterialTheme.colorScheme.onSurfaceVariant
    }
    if (line.level.isEmpty()) Text(line.raw, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
    else {
        val stamp: @Composable () -> Unit = {
            Column {
                Text(line.time, style = MaterialTheme.typography.labelSmall, fontFamily = FontFamily.Monospace,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                Text(listOf(if (marks) line.mark else "", line.level).filter { it.isNotEmpty() }.joinToString(" "),
                    color = color, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.labelSmall)
            }
        }
        val message: @Composable (Modifier) -> Unit = { modifier ->
            Column(modifier) {
                Text(line.message, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall,
                    maxLines = if (expanded) Int.MAX_VALUE else 3)
                if (expanded) CopyButton("log line", line.raw)
            }
        }
        val modifier = Modifier.fillMaxWidth().clickable(onClickLabel = if (expanded) "Collapse log line" else "Show whole log line") { expanded = !expanded }
            .padding(vertical = 4.dp)
        if (LocalConfiguration.current.fontScale >= 1.3f) Column(modifier, verticalArrangement = Arrangement.spacedBy(4.dp)) {
            stamp(); message(Modifier.fillMaxWidth())
        } else Row(modifier, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            stamp(); message(Modifier.weight(1f))
        }
    }
}
