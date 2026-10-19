import { type DataType, type Table, Type } from "apache-arrow";
import { ArrowDown, ArrowUp, Search, X } from "lucide-react";
import {
  type KeyboardEvent,
  type MouseEvent,
  type PointerEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { copyText } from "../lib/clipboard";
import { comparatorFor, sortRows } from "../lib/compare";
import { columnText, isNumeric, jsonFor, typeLabel } from "../lib/format";
import { RowTexts } from "../lib/rowText";
import {
  type ClipboardValue,
  type GridCell,
  type GridSelection,
  moveFocus,
  rectContains,
  selectionRect,
  toClipboardHtml,
  toClipboardText,
} from "../lib/gridSelection";
import { CellInspector, type InspectedCell } from "./CellInspector";

const rowHeight = 26;
const headerHeight = 26;
const overscan = 10;
const rowNumberWidth = 56;
const minAutoWidth = 64;
const maxAutoWidth = 420;
const resizeMin = 40;
const resizeMax = 2000;
const resizeKeyStep = 16;
const charPx = 7.4;
const cellPad = 18;
const widthSampleRows = 200;
/** Text drawn in a cell; the inspector shows the whole value. */
const cellTextCap = 400;
/** A copy bigger than this is refused (building it would stall the browser): use an export. */
const maxCopyCells = 1_000_000;
/** A drag this close to (or past) an edge of the grid scrolls it. */
const dragEdgePx = 16;

type SortDirection = "asc" | "desc";

interface Props {
  table: Table;
  /** Buttons at the start of the toolbar's right-hand group (Copy, Export, Expand…). */
  toolbar?: (shown: readonly number[]) => ReactNode;
  /** Something to tell the user (a copy, a failure), shown in the toolbar. */
  onNotice?: (text: string) => void;
}

const plural = (n: number, word: string) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
const isNull = (v: unknown) => v === null || v === undefined;

/**
 * The results grid. Only the rows in view are in the document, so a million rows scroll like
 * ten. Click a column header to sort (again for descending, a third time to clear; NULLs last);
 * type in the filter to keep rows where any column contains the text. Drag a header's edge to
 * resize its column (double-click it to fit, or use the arrow keys on it).
 *
 * Cells select like a spreadsheet: a click selects a cell; the arrow keys move it, Home and End
 * go to the row's ends, Page Up and Page Down a screenful, Ctrl/⌘ with any of them to the edge;
 * Shift (with keys or a click) or a drag selects a range, and the grid scrolls when the drag
 * passes an edge; row numbers select rows; Ctrl/⌘+A selects every row shown. Ctrl/⌘+C copies
 * the selection (full values, NULL as an empty cell) as text and as an HTML table, so it
 * pastes into Excel or Google Sheets as cells. Double-click or Enter shows a value in full.
 */
export function ResultsGrid({ table, toolbar, onNotice }: Props) {
  const fields = table.schema.fields;
  const columns = fields.length;
  const vectors = useMemo(() => fields.map((_, i) => table.getChildAt(i)!), [table, fields]);
  const formats = useMemo(() => columnText(table.schema), [table]);
  const numeric = useMemo(() => fields.map((f) => isNumeric(f.type as DataType)), [fields]);
  const labels = useMemo(() => fields.map((f) => typeLabel(f)), [fields]);

  // Each row's display text, made when first needed; a new table brings new texts.
  const texts = useMemo(() => new RowTexts(table), [table]);
  const rowText = useCallback((r: number) => texts.row(r), [texts]);

  // ── Column widths: fitted to a sample of the rows, or set by hand ─────────────────────────
  const fitted = useMemo(() => {
    const sample = Math.min(table.numRows, widthSampleRows);
    return fields.map((f, c) => {
      let chars = f.name.length + labels[c]!.length * 0.85 + 2;
      for (let r = 0; r < sample; r++) {
        const t = rowText(r)[c] ?? null;
        chars = Math.max(chars, Math.min(t === null ? 4 : t.length, cellTextCap));
      }
      return Math.round(Math.max(minAutoWidth, Math.min(maxAutoWidth, chars * charPx + cellPad)));
    });
  }, [table, fields, labels, rowText]);
  const [manual, setManual] = useState<Record<number, number>>({});
  const widths = fitted.map((w, c) => manual[c] ?? w);
  const lefts = useMemo(() => {
    const out: number[] = [];
    let x = 0;
    for (const w of widths) {
      out.push(x);
      x += w;
    }
    return out;
  }, [widths]);
  const totalWidth = rowNumberWidth + widths.reduce((a, b) => a + b, 0);
  const setWidth = (c: number, px: number | null) =>
    setManual((m) => {
      const { [c]: _old, ...rest } = m;
      return px === null ? rest : { ...rest, [c]: Math.round(Math.max(resizeMin, Math.min(resizeMax, px))) };
    });

  // ── Filter and sort: the row order shown ──────────────────────────────────────────────────
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState<{ col: number; dir: SortDirection } | null>(null);
  const shown = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    let rows: number[];
    if (needle) {
      rows = [];
      for (let r = 0; r < table.numRows; r++) {
        if (rowText(r).some((t) => (t ?? "NULL").toLowerCase().includes(needle))) rows.push(r);
      }
    } else {
      rows = Array.from({ length: table.numRows }, (_, i) => i);
    }
    if (sort) {
      const vector = vectors[sort.col]!;
      const compare = comparatorFor(fields[sort.col]!.type as DataType, formats[sort.col]!);
      rows = sortRows(rows, (r) => vector.get(r), compare, sort.dir);
    }
    return rows;
  }, [table, filter, sort, vectors, fields, formats, rowText]);
  const rowCount = shown.length;

  const toggleSort = (col: number) =>
    setSort((s) => (!s || s.col !== col ? { col, dir: "asc" } : s.dir === "asc" ? { col, dir: "desc" } : null));

  // ── Scrolling and the rows in view ────────────────────────────────────────────────────────
  const scroller = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewHeight, setViewHeight] = useState(400);
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setViewHeight(el.clientHeight));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  const first = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
  const last = Math.min(rowCount - 1, Math.ceil((scrollTop + viewHeight) / rowHeight) + overscan);

  // ── Selection: positions in `shown`; a new order (sort, filter) starts it over ────────────
  const [selected, setSelected] = useState<{ order: readonly number[]; sel: GridSelection } | null>(null);
  const sel = selected?.order === shown ? selected.sel : null;
  const rect = sel ? selectionRect(sel) : null;
  const gridId = useId();
  const cellId = (row: number, col: number) => `${gridId}-r${row}-c${col}`;

  /** Scrolls so the cell is wholly in view, below the header and right of the row numbers. */
  const scrollCellIntoView = (cell: GridCell) => {
    const el = scroller.current;
    if (!el) return;
    const top = headerHeight + cell.row * rowHeight;
    if (top < el.scrollTop + headerHeight) el.scrollTop = top - headerHeight;
    else if (top + rowHeight > el.scrollTop + el.clientHeight) el.scrollTop = top + rowHeight - el.clientHeight;
    const left = rowNumberWidth + lefts[cell.col]!;
    const right = left + widths[cell.col]!;
    if (left < el.scrollLeft + rowNumberWidth) el.scrollLeft = left - rowNumberWidth;
    else if (right > el.scrollLeft + el.clientWidth)
      el.scrollLeft = Math.min(left - rowNumberWidth, right - el.clientWidth);
  };

  // After a keyboard move, bring the active cell into the page's view too, once its row exists
  // (rows are drawn only when in view).
  const reveal = useRef<string | null>(null);
  const select = (next: GridSelection, scroll = true) => {
    setSelected({ order: shown, sel: next });
    reveal.current = null;
    if (scroll) {
      scrollCellIntoView(next.focus);
      reveal.current = cellId(next.focus.row, next.focus.col);
    }
  };
  useLayoutEffect(() => {
    const target = reveal.current ? document.getElementById(reveal.current) : null;
    if (!target) return;
    reveal.current = null;
    target.scrollIntoView({ block: "nearest", inline: "nearest" });
  });

  /** The cell under a point, clamped to the grid (over the header or row numbers: the first one). */
  const cellAt = (x: number, y: number): GridCell => {
    const el = scroller.current!;
    const box = el.getBoundingClientRect();
    const top = Math.max(y - box.top, headerHeight) - headerHeight + el.scrollTop;
    const left = Math.max(x - box.left, rowNumberWidth) - rowNumberWidth + el.scrollLeft;
    const row = Math.max(0, Math.min(rowCount - 1, Math.floor(top / rowHeight)));
    let col = 0;
    while (col < columns - 1 && left >= lefts[col + 1]!) col++;
    return { row, col };
  };

  const rowsSelection = (anchorRow: number, focusRow: number): GridSelection => ({
    anchor: { row: anchorRow, col: 0 },
    focus: { row: focusRow, col: columns - 1 },
  });

  // A press on a cell or a row number selects it (Shift: extends from the anchor); dragging
  // extends to the cell under the pointer, scrolling while the pointer is at or past an edge.
  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || rowCount === 0 || columns === 0) return;
    const target = e.target as HTMLElement;
    const onRowNumber = !!target.closest("[data-rownum]");
    if (!onRowNumber && !target.closest('[role="gridcell"]')) return; // header, handles, scrollbars
    const el = e.currentTarget;
    el.focus({ preventScroll: true });
    const hit = cellAt(e.clientX, e.clientY);
    const next = onRowNumber
      ? rowsSelection(e.shiftKey && sel ? sel.anchor.row : hit.row, hit.row)
      : { anchor: e.shiftKey && sel ? sel.anchor : hit, focus: hit };
    select(next, false);
    const anchor = next.anchor;
    const pointer = { x: e.clientX, y: e.clientY };
    let frame = 0;
    const extend = () => {
      const at = cellAt(pointer.x, pointer.y);
      select(onRowNumber ? rowsSelection(anchor.row, at.row) : { anchor, focus: at }, false);
    };
    const speed = (d: number) => Math.sign(d) * Math.min(40, Math.max(4, Math.abs(d) / 2));
    const autoScroll = () => {
      const box = el.getBoundingClientRect();
      const top = box.top + headerHeight + dragEdgePx;
      const bottom = box.bottom - dragEdgePx;
      const left = box.left + rowNumberWidth + dragEdgePx;
      const right = box.right - dragEdgePx;
      const dy = pointer.y < top ? pointer.y - top : pointer.y > bottom ? pointer.y - bottom : 0;
      const dx = onRowNumber ? 0 : pointer.x < left ? pointer.x - left : pointer.x > right ? pointer.x - right : 0;
      if (dx || dy) {
        el.scrollTop += dy ? speed(dy) : 0;
        el.scrollLeft += dx ? speed(dx) : 0;
        extend();
      }
      frame = requestAnimationFrame(autoScroll);
    };
    const move = (ev: globalThis.PointerEvent) => {
      pointer.x = ev.clientX;
      pointer.y = ev.clientY;
      extend();
    };
    const up = (ev: globalThis.PointerEvent) => {
      cancelAnimationFrame(frame);
      if (el.hasPointerCapture(ev.pointerId)) el.releasePointerCapture(ev.pointerId);
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", up);
      el.removeEventListener("pointercancel", up);
    };
    el.setPointerCapture(e.pointerId);
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
    frame = requestAnimationFrame(autoScroll);
  };

  // No text selection or focus jump from pressing a cell: the grid takes focus itself.
  const onMouseDown = (e: MouseEvent<HTMLDivElement>) => {
    const target = e.target as HTMLElement;
    if (target.closest('[role="gridcell"]') || target.closest("[data-rownum]")) e.preventDefault();
  };

  // ── The cell inspector ────────────────────────────────────────────────────────────────────
  const [inspected, setInspected] = useState<InspectedCell | null>(null);
  const inspect = (position: number, col: number) => {
    const r = shown[position]!;
    const raw: unknown = vectors[col]!.get(r);
    const type = fields[col]!.type as DataType;
    const nested =
      !isNull(raw) && [Type.List, Type.LargeList, Type.FixedSizeList, Type.Struct, Type.Map].includes(type.typeId);
    setInspected({
      column: fields[col]!.name,
      type: labels[col]!,
      row: position + 1,
      text: rowText(r)[col] ?? null,
      json: nested ? jsonFor(type)(raw) : undefined,
    });
  };

  // Double-click shows the value. It's handled on the grid, by position: the press took
  // pointer capture, so the browser sends the clicks to the grid, not to the cell.
  const onDoubleClick = (e: MouseEvent<HTMLDivElement>) => {
    const el = scroller.current;
    if (!el || rowCount === 0 || columns === 0) return;
    const box = el.getBoundingClientRect();
    const x = e.clientX - box.left;
    const y = e.clientY - box.top;
    if (y < headerHeight || x < rowNumberWidth || x >= el.clientWidth || y >= el.clientHeight) return;
    if (y - headerHeight + el.scrollTop >= rowCount * rowHeight) return; // below the last row
    if (x - rowNumberWidth + el.scrollLeft >= totalWidth - rowNumberWidth) return; // past the last column
    const at = cellAt(e.clientX, e.clientY);
    inspect(at.row, at.col);
  };

  // ── Copy ──────────────────────────────────────────────────────────────────────────────────
  const copySelection = async () => {
    if (!rect) return;
    const cells = (rect.bottom - rect.top + 1) * (rect.right - rect.left + 1);
    if (cells > maxCopyCells) {
      onNotice?.(`${cells.toLocaleString()} cells is more than the browser can copy: select fewer, or export.`);
      return;
    }
    const values: ClipboardValue[][] = [];
    for (let p = rect.top; p <= rect.bottom; p++) {
      const texts = rowText(shown[p]!);
      values.push(texts.slice(rect.left, rect.right + 1));
    }
    const textColumns = fields
      .slice(rect.left, rect.right + 1)
      .map((f) => [Type.Utf8, Type.LargeUtf8].includes((f.type as DataType).typeId));
    const ok = await copyText(toClipboardText(values), toClipboardHtml(values, textColumns));
    onNotice?.(ok ? `Copied ${plural(cells, "cell")}.` : "The browser refused the copy.");
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget || rowCount === 0 || columns === 0) return; // header buttons, handles
    const mod = e.ctrlKey || e.metaKey;
    const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    if (mod && key === "a") {
      e.preventDefault();
      select({ anchor: { row: 0, col: 0 }, focus: { row: rowCount - 1, col: columns - 1 } }, false);
      return;
    }
    if (mod && key === "c") {
      if (sel) {
        e.preventDefault();
        void copySelection();
      }
      return;
    }
    if (key === "Enter" && sel) {
      e.preventDefault();
      inspect(sel.focus.row, sel.focus.col);
      return;
    }
    if (key === "Escape" && sel && (sel.anchor.row !== sel.focus.row || sel.anchor.col !== sel.focus.col)) {
      e.preventDefault();
      select({ anchor: sel.focus, focus: sel.focus });
      return;
    }
    const el = scroller.current;
    const pageRows = Math.max(1, Math.floor(((el?.clientHeight ?? 400) - headerHeight) / rowHeight) - 1);
    const moved = moveFocus({
      key,
      ctrlOrMeta: mod,
      focus: sel?.focus ?? { row: 0, col: 0 },
      rowCount,
      colCount: columns,
      pageRows,
    });
    if (!moved) return;
    e.preventDefault();
    if (!sel) {
      select({ anchor: { row: 0, col: 0 }, focus: { row: 0, col: 0 } });
      return;
    }
    select(e.shiftKey ? { anchor: sel.anchor, focus: moved } : { anchor: moved, focus: moved });
  };

  // ── Column resizing ───────────────────────────────────────────────────────────────────────
  const startResize = (e: PointerEvent<HTMLDivElement>, c: number) => {
    e.preventDefault();
    e.stopPropagation();
    const handle = e.currentTarget;
    const startX = e.clientX;
    const start = widths[c]!;
    handle.setPointerCapture(e.pointerId);
    const move = (ev: globalThis.PointerEvent) => setWidth(c, start + ev.clientX - startX);
    const up = (ev: globalThis.PointerEvent) => {
      if (handle.hasPointerCapture(ev.pointerId)) handle.releasePointerCapture(ev.pointerId);
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  };

  const selectionLabel =
    rect && (rect.bottom > rect.top || rect.right > rect.left)
      ? `${plural(rect.bottom - rect.top + 1, "row")} × ${plural(rect.right - rect.left + 1, "column")} selected`
      : null;
  const focusDrawn = !!sel && sel.focus.row >= first && sel.focus.row <= last;

  const body = [];
  for (let p = first; p <= last; p++) {
    const r = shown[p]!;
    const texts = rowText(r);
    const rowInSelection = !!rect && p >= rect.top && p <= rect.bottom;
    const cells = [];
    for (let c = 0; c < columns; c++) {
      const t = texts[c] ?? null;
      const isSelected = !!rect && rectContains(rect, p, c);
      const active = !!sel && sel.focus.row === p && sel.focus.col === c;
      const shownText = t === null ? "NULL" : t.length > cellTextCap ? `${t.slice(0, cellTextCap)}…` : t;
      cells.push(
        <div
          key={c}
          id={cellId(p, c)}
          role="gridcell"
          aria-colindex={c + 2}
          aria-selected={isSelected}
          data-active={active || undefined}
          className={`grid-cell${numeric[c] ? " grid-num" : ""}${t === null ? " grid-null" : ""}${isSelected ? " grid-selected" : ""}${active ? " grid-active" : ""}`}
          style={{ width: widths[c], scrollMarginTop: headerHeight, scrollMarginLeft: rowNumberWidth }}
          title={
            t !== null && t.length > 24
              ? t.length > cellTextCap
                ? "Double-click to see the whole value"
                : t
              : undefined
          }
        >
          {shownText}
        </div>,
      );
    }
    body.push(
      <div
        key={p}
        role="row"
        aria-rowindex={p + 2}
        className={`grid-row${p % 2 ? " grid-row-odd" : ""}`}
        style={{ transform: `translateY(${headerHeight + p * rowHeight}px)`, width: totalWidth }}
      >
        <div
          role="rowheader"
          data-rownum=""
          className={`grid-cell grid-rownum${rowInSelection ? " grid-selected" : ""}`}
          style={{ width: rowNumberWidth }}
          title="Click to select the row · drag for several"
        >
          {(p + 1).toLocaleString()}
        </div>
        {cells}
      </div>,
    );
  }

  return (
    <div className="results-grid-pane">
      <div className="result-toolbar">
        <div className="grid-filter">
          <Search size={13} aria-hidden />
          <input
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter rows…"
            aria-label="Filter rows"
            data-testid="results-filter"
          />
          {filter && (
            <button
              type="button"
              className="grid-filter-clear"
              aria-label="Clear the filter"
              onClick={() => setFilter("")}
            >
              <X size={12} />
            </button>
          )}
        </div>
        <span className="results-count" data-testid="results-count">
          {filter
            ? `${rowCount.toLocaleString()} of ${plural(table.numRows, "row")} match`
            : plural(table.numRows, "row")}
          {sort && ` · sorted by ${fields[sort.col]!.name} ${sort.dir === "asc" ? "ascending" : "descending"}`}
          {selectionLabel && <span data-testid="results-selection"> · {selectionLabel}</span>}
        </span>
        <span className="toolbar-spacer" />
        {toolbar?.(shown)}
      </div>
      <div
        ref={scroller}
        className="grid"
        role="grid"
        aria-label="Results"
        aria-rowcount={rowCount + 1}
        aria-colcount={columns + 1}
        aria-multiselectable="true"
        aria-activedescendant={focusDrawn ? cellId(sel.focus.row, sel.focus.col) : undefined}
        tabIndex={0}
        onKeyDown={onKeyDown}
        onPointerDown={onPointerDown}
        onMouseDown={onMouseDown}
        onDoubleClick={onDoubleClick}
        onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
        data-testid="results-grid"
      >
        <div className="grid-canvas" style={{ height: headerHeight + rowCount * rowHeight, width: totalWidth }}>
          <div role="row" aria-rowindex={1} className="grid-row grid-header" style={{ width: totalWidth }}>
            <div role="columnheader" className="grid-cell grid-rownum grid-corner" style={{ width: rowNumberWidth }}>
              #
            </div>
            {fields.map((f, c) => {
              const sorted = sort?.col === c ? sort.dir : undefined;
              const inSelection = !!rect && c >= rect.left && c <= rect.right;
              return (
                <div
                  key={c}
                  role="columnheader"
                  aria-colindex={c + 2}
                  aria-sort={sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : undefined}
                  className={`grid-cell grid-head${numeric[c] ? " grid-num" : ""}${inSelection ? " grid-head-selected" : ""}`}
                  style={{ width: widths[c] }}
                >
                  <button
                    type="button"
                    className="grid-sort"
                    title={`${f.name}: ${labels[c]}. Click to sort`}
                    onClick={() => toggleSort(c)}
                  >
                    <span className="grid-head-name">{f.name}</span>
                    <span className="grid-head-type">{labels[c]}</span>
                    {sorted === "asc" && <ArrowUp size={12} aria-label="ascending" />}
                    {sorted === "desc" && <ArrowDown size={12} aria-label="descending" />}
                  </button>
                  <div
                    className="grid-resize"
                    role="separator"
                    aria-orientation="vertical"
                    aria-label={`Resize column ${f.name}`}
                    aria-valuenow={widths[c]}
                    tabIndex={0}
                    title="Drag to resize · double-click to fit"
                    data-testid={`column-resize-${c}`}
                    onPointerDown={(e) => startResize(e, c)}
                    onClick={(e) => e.stopPropagation()}
                    onDoubleClick={(e) => {
                      e.stopPropagation();
                      setWidth(c, null);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
                        e.preventDefault();
                        setWidth(c, widths[c]! + (e.key === "ArrowRight" ? resizeKeyStep : -resizeKeyStep));
                      }
                    }}
                  />
                </div>
              );
            })}
          </div>
          {body}
        </div>
        {table.numRows === 0 && <div className="grid-empty">No rows</div>}
        {table.numRows > 0 && rowCount === 0 && <div className="grid-empty">No rows match the filter.</div>}
      </div>
      {inspected && (
        <CellInspector
          cell={inspected}
          onClose={() => {
            setInspected(null);
            scroller.current?.focus({ preventScroll: true });
          }}
        />
      )}
    </div>
  );
}
