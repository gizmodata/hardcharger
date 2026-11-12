package com.gizmodata.hardcharger

import org.junit.Assert.*
import org.junit.Test

class InputSafetyTest {
    @Test fun importsDoNotTurnFilenamesIntoSql() {
        assertEquals("sales_2026", tableName("Sales 2026.csv"))
        assertEquals("t_2026_results", tableName("2026 Résults.parquet"))
        assertEquals("'a''; DROP TABLE t;--'", sqlString("a'; DROP TABLE t;--"))
        assertEquals("\"a\"\"b\"", sqlIdentifier("a\"b"))
        assertEquals("read_json_auto", importReader("events.NDJSON"))
    }
    @Test fun rejectsUnsupportedAndInvalidConfiguration() {
        assertThrows(IllegalStateException::class.java) { importReader("script.sh") }
        assertThrows(IllegalArgumentException::class.java) { ServerSettings(port = 80).validate() }
        assertThrows(IllegalArgumentException::class.java) { ServerSettings(memoryMB = -1).validate() }
        assertThrows(IllegalArgumentException::class.java) { ServerSettings(threads = -1).validate() }
    }
    @Test fun endpointNormalizationDoesNotCarrySecretsOrPaths() {
        assertEquals("https://db.example:31337", Endpoint.parse("db.example:31337/ui/?token=bad").origin)
        assertEquals("https://db.example", Endpoint.parse("https://db.example:443/ui/").origin)
        assertThrows(IllegalArgumentException::class.java) { Endpoint.parse("https://user:secret@db.example") }
        assertThrows(IllegalArgumentException::class.java) { Endpoint.parse("file:///etc/passwd") }
    }
}
