// Cell selection in the results grid, like a spreadsheet's: a click selects a cell, the arrow
// keys move it, Shift (with keys or a click) and a mouse drag select a rectangle, and
// Ctrl/⌘+C copies the rectangle so it pastes into Excel or Google Sheets as cells.
//
// Rows are positions in the order shown (after the filter and the sort), not Arrow row indexes.

export interface GridCell {
  row: number;
  col: number;
}

/** The cell the selection started from (anchor) and the one it reaches (focus, the active cell). */
export interface GridSelection {
  anchor: GridCell;
  focus: GridCell;
}

export interface GridRect {
  top: number;
  bottom: number;
  left: number;
  right: number;
}

export const selectionRect = ({ anchor, focus }: GridSelection): GridRect => ({
  top: Math.min(anchor.row, focus.row),
  bottom: Math.max(anchor.row, focus.row),
  left: Math.min(anchor.col, focus.col),
  right: Math.max(anchor.col, focus.col),
});

export const rectContains = (rect: GridRect, row: number, col: number): boolean =>
  row >= rect.top && row <= rect.bottom && col >= rect.left && col <= rect.right;

const clamp = (value: number, max: number) => Math.max(0, Math.min(max, value));

/**
 * Where a navigation key moves the active cell, or null when the key doesn't navigate. Arrows
 * move one cell (Ctrl/⌘: to that edge), Home and End go to the row's first and last column
 * (Ctrl/⌘: the grid's first and last cell), Page Up and Page Down move a screenful of rows.
 */
export function moveFocus(options: {
  key: string;
  ctrlOrMeta: boolean;
  focus: GridCell;
  rowCount: number;
  colCount: number;
  pageRows: number;
}): GridCell | null {
  const { key, ctrlOrMeta, focus, rowCount, colCount, pageRows } = options;
  const lastRow = rowCount - 1;
  const lastCol = colCount - 1;
  switch (key) {
    case "ArrowUp":
      return { row: ctrlOrMeta ? 0 : clamp(focus.row - 1, lastRow), col: focus.col };
    case "ArrowDown":
      return { row: ctrlOrMeta ? lastRow : clamp(focus.row + 1, lastRow), col: focus.col };
    case "ArrowLeft":
      return { row: focus.row, col: ctrlOrMeta ? 0 : clamp(focus.col - 1, lastCol) };
    case "ArrowRight":
      return { row: focus.row, col: ctrlOrMeta ? lastCol : clamp(focus.col + 1, lastCol) };
    case "Home":
      return ctrlOrMeta ? { row: 0, col: 0 } : { row: focus.row, col: 0 };
    case "End":
      return ctrlOrMeta ? { row: lastRow, col: lastCol } : { row: focus.row, col: lastCol };
    case "PageUp":
      return { row: clamp(focus.row - pageRows, lastRow), col: focus.col };
    case "PageDown":
      return { row: clamp(focus.row + pageRows, lastRow), col: focus.col };
    default:
      return null;
  }
}

/** A cell for the clipboard: its full text, or null for SQL NULL (pasted as an empty cell). */
export type ClipboardValue = string | null;

const tsvField = (value: ClipboardValue): string => {
  if (value === null) return "";
  return /[\t\n\r"]/.test(value) ? `"${value.replaceAll('"', '""')}"` : value;
};

/**
 * The plain-text clipboard form. One cell is its value as it is (so it pastes cleanly anywhere);
 * a range is tab-separated columns and newline-separated rows, a value holding a tab, a line
 * break or a quote quoted the way Excel and Google Sheets read it back into one cell.
 */
export function toClipboardText(values: ClipboardValue[][]): string {
  if (values.length === 1 && values[0]!.length === 1) return values[0]![0] ?? "";
  return values.map((row) => row.map(tsvField).join("\t")).join("\n");
}

const escapeHtml = (text: string): string =>
  text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");

/**
 * The HTML clipboard form, which Excel prefers to the text: a `<td>` per cell, so a value with
 * line breaks stays in one cell, and text columns marked as text so a value like `00123` keeps
 * its leading zeros.
 */
export function toClipboardHtml(values: ClipboardValue[][], textColumns: readonly boolean[]): string {
  const rows = values.map(
    (row) =>
      `<tr>${row
        .map((value, c) => {
          const style = textColumns[c] ? " style=\"mso-number-format:'\\@'\"" : "";
          const body =
            value === null ? "" : escapeHtml(value).replace(/\r?\n/g, '<br style="mso-data-placement:same-cell">');
          return `<td${style}>${body}</td>`;
        })
        .join("")}</tr>`,
  );
  return `<meta charset="utf-8"><table><tbody>${rows.join("")}</tbody></table>`;
}
