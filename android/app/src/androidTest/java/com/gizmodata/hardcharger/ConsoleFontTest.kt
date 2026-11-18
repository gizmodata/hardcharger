package com.gizmodata.hardcharger

import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * #57: the console draws a boxed result table with every line the same width, so its borders
 * meet its rows. Android's own monospace font has no box-drawing characters; they came from a
 * fallback font of another width, and the borders ran past the rows.
 */
@RunWith(AndroidJUnit4::class)
class ConsoleFontTest {
    @get:Rule val compose = createComposeRule()

    /** A table as the console prints it (duckbox), every line the same number of characters. */
    private val table = listOf(
        "┌───────┬─────────┬──────────────┐",
        "│   n   │  label  │    amount    │",
        "│ int64 │ varchar │ decimal(9,2) │",
        "├───────┼─────────┼──────────────┤",
        "│     1 │ a       │        12.50 │",
        "│     2 │ b…      │         0.00 │",
        "└───────┴─────────┴──────────────┘",
        "╔═══╦═══╗ ╭───╮ xxxxxxxxxxxxxxxxxx",
    )

    /** Each text's width in each style, measured in one composition. */
    private fun widths(styles: List<@Composable () -> TextStyle>, texts: List<String>): List<List<Int>> {
        var measured = emptyList<List<Int>>()
        compose.setContent {
            val measurer = rememberTextMeasurer()
            measured = styles.map { style ->
                val textStyle = style()
                texts.map { measurer.measure(it, textStyle, softWrap = false).size.width }
            }
        }
        compose.waitForIdle()
        return measured
    }

    @Test fun boxDrawingCharactersHaveTheTextsAdvance() {
        val glyphs = listOf("x", "─", "│", "┌", "┬", "┐", "├", "┼", "┤", "└", "┴", "┘", "═", "║", "╭", "…")
        val runs = glyphs.map { it.repeat(40) }
        val measured = widths(listOf { MaterialTheme.typography.bodySmall.copy(fontFamily = ConsoleFont) }, runs).single()
        val text = measured.first()
        glyphs.zip(measured).forEach { (glyph, width) ->
            assertEquals("40 × '$glyph' is as wide as 40 × 'x' in the console's font", text, width)
        }
    }

    @Test fun aBoxedTablesBordersAreAsWideAsItsRows() {
        table.forEach { assertEquals(it, table.first().length, it.length) }
        val measured = widths(listOf(
            { MaterialTheme.typography.bodySmall.copy(fontFamily = ConsoleFont) },   // the transcript
            { MaterialTheme.typography.bodyMedium.copy(fontFamily = ConsoleFont) },  // the input
        ), table)
        for (lines in measured) {
            lines.forEachIndexed { i, width -> assertEquals("line ${i + 1}: ${table[i]}", lines.first(), width) }
        }
    }
}
