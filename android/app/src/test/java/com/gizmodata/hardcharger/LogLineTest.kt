package com.gizmodata.hardcharger

import org.junit.Assert.*
import org.junit.Test

class LogLineTest {
    @Test fun markedLogsAndMultilineStatementsKeepTheirOriginalText() {
        val raw = "2026-11-12T14:00:12.123Z ⚠️ warning Slow query\nSELECT\n  42;"
        val line = LogLine.parse(raw).single()
        assertEquals(raw, line.raw)
        assertEquals("14:00:12.123", line.time)
        assertEquals("warning", line.level)
        assertEquals("Slow query\nSELECT\n  42;", line.message)
        assertEquals("⚠️", line.mark)
    }
    @Test fun plainLogsReceiveConsistentStatusMarks() {
        val lines = LogLine.parse("2026-11-12T14:00:12Z info Hardcharger is ready port=31337\n2026-11-12T14:00:13Z error Failed")
        assertEquals("✅", lines[0].mark)
        assertEquals("❌", lines[1].mark)
        assertFalse(lines[1].success)
    }
    @Test fun UnstructuredOutputIsPreserved() {
        val text = "DuckDB startup banner"
        assertEquals(LogLine(text), LogLine.parse(text).single())
    }
}
