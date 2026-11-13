package com.gizmodata.hardcharger

import org.junit.Assert.*
import org.junit.Test

class ResourceSettingsTest {
    @Test fun freshInstallationDefaultsMatchIos() {
        val settings = ServerSettings()
        assertEquals(31337, settings.port)
        assertTrue(settings.autoStart)
        assertTrue(settings.keepAwake)
        assertTrue(settings.printQueries)
        assertEquals("info", settings.logLevel)
        assertFalse(settings.share)
        assertFalse(settings.tls)
    }
    @Test fun automaticMemoryMatchesIosPolicyAndIsBounded() {
        val automatic = ServerSettings()
        automatic.validate()
        assertEquals(0, automatic.memoryMB)
        assertEquals(256, automatic.resolvedMemoryMB(512L * 1024 * 1024))
        assertEquals(1228, automatic.resolvedMemoryMB(4096L * 1024 * 1024))
        assertEquals(4096, automatic.resolvedMemoryMB(32768L * 1024 * 1024))
        assertEquals(256, automatic.resolvedMemoryMB(0))
    }
    @Test fun automaticThreadsResolveToAtLeastOneCore() {
        assertEquals(0, ServerSettings().threads)
        assertEquals(8, ServerSettings().resolvedThreads(8))
        assertEquals(1, ServerSettings().resolvedThreads(0))
    }
    @Test fun fixedLimitsRemainExact() {
        val fixed = ServerSettings(memoryMB = 384, threads = 3)
        fixed.validate()
        assertEquals(384, fixed.resolvedMemoryMB(8L * 1024 * 1024 * 1024))
        assertEquals(3, fixed.resolvedThreads(8))
    }
}
