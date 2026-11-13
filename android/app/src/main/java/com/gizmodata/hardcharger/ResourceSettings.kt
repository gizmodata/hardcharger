package com.gizmodata.hardcharger

import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.UnfoldMore
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp

private val memoryChoices = linkedMapOf(0 to "Automatic", 512 to "512 MB", 1024 to "1 GB",
    2048 to "2 GB", 4096 to "4 GB", 8192 to "8 GB")

@Composable fun MemoryLimitPicker(value: String, change: (String) -> Unit) {
    var custom by rememberSaveable { mutableStateOf(value.toIntOrNull() !in memoryChoices) }
    SettingPicker("Memory limit", if (custom) "Custom…" else memoryChoices[value.toIntOrNull()] ?: "$value MB") { close ->
        memoryChoices.forEach { (mb, label) -> DropdownMenuItem(text = { Text(label) }, onClick = {
            custom = false; change(mb.toString()); close()
        }) }
        DropdownMenuItem(text = { Text("Custom…") }, onClick = {
            custom = true; if (value == "0") change("512"); close()
        })
    }
    if (custom) NumberRow("Custom limit (MB)", value, change)
}

@Composable fun ThreadsPicker(value: Int, processors: Int, change: (Int) -> Unit) {
    SettingPicker("Threads", if (value == 0) "One per core" else value.toString()) { close ->
        // Preserve existing explicit limits, including values above the physical core count.
        (listOf(0) + (1..processors.coerceIn(1, 64)) + value).distinct().sorted().forEach { threads ->
            DropdownMenuItem(text = { Text(if (threads == 0) "One per core" else threads.toString()) },
                onClick = { change(threads); close() })
        }
    }
}

@Composable private fun SettingPicker(label: String, value: String,
    choices: @Composable ColumnScope.(close: () -> Unit) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    Row(Modifier.fillMaxWidth().padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(label, Modifier.weight(1f).padding(end = 8.dp), style = MaterialTheme.typography.bodyLarge)
        Box {
            TextButton(onClick = { expanded = true }, modifier = Modifier.semantics { contentDescription = "$label setting" }) {
                Text(value); Icon(Icons.Outlined.UnfoldMore, null, Modifier.size(18.dp))
            }
            DropdownMenu(expanded, { expanded = false }) { choices { expanded = false } }
        }
    }
}
