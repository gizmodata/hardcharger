package com.gizmodata.hardcharger

enum class LogStyle(val label: String) { Emoji("Emoji"), Plain("Plain") }

/** The same timestamp/optional mark/level format used by the server and iOS log view. */
data class LogLine(val raw: String, val time: String = "", val level: String = "", val message: String = raw) {
    val success get() = level == "info" && (message.startsWith("Hardcharger is ready") || message == "stopped")
    val mark get() = when { success -> "✅"; level == "warning" -> "⚠️"; level == "error" -> "❌"; else -> "" }
    companion object {
        private val pattern = Regex("^(\\S{19,}Z)\\s+(?:(?:✅|💥|⚠️|❌)\\s+)?(debug|info|warning|error)(?:\\s+(.*))?$")
        fun parse(text: String): List<LogLine> {
            val lines = mutableListOf<LogLine>()
            text.lineSequence().forEach { raw ->
                val match = pattern.matchEntire(raw)
                if (match != null) lines += LogLine(raw, match.groupValues[1].substringAfter('T').removeSuffix("Z"),
                    match.groupValues[2], match.groupValues[3])
                else if (lines.lastOrNull()?.level?.isNotEmpty() == true) {
                    val last = lines.removeAt(lines.lastIndex)
                    lines += last.copy(raw = last.raw + "\n" + raw, message = last.message + "\n" + raw)
                } else lines += LogLine(raw)
            }
            return lines
        }
    }
}
