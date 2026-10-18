import type { DataType, Table } from "apache-arrow";
import { type KeyboardEvent, type PointerEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { copyText } from "../lib/clipboard";
import { toDelimited } from "../lib/export";
import { columnText, isNumeric, typeLabel } from "../lib/format";
import { ValueDialog } from "./ValueDialog";

const rowHeight = 26;
const overscan = 8;
const numberColumnWidth = 56;

type Cell = [row: number, col: number];
interface Selection {
  anchor: Cell;
  focus: Cell;
}

function bounds(s: Selection): { r0: number; r1: number; c0: number; c1: number } {
  return {
    r0: Math.min(s.anchor[0], s.focus[0]),
    r1: Math.max(s.anchor[0], s.focus[0]),
    c0: Math.min(s.anchor[1], s.focus[1]),
    c1: Math.max(s.anchor[1], s.focus[1]),
  };
}

interface Props {
  table: Table;
  /** Called after a copy: how many cells went to the clipboard, or false when it failed. */
  onCopied?: (cells: number | false) => void;
}

/**
 * The results grid: only the rows in view are in the document, so a million rows scroll as
 * smoothly as ten. Click a cell (shift-click, or shift and the arrow keys, for a range), a
 * column header or a row number to select; Ctrl/⌘+C copies the selection as tab-separated
 * text, Ctrl/⌘+A selects everything; double-click shows a whole value.
 */
export function ResultsGrid({ table, onCopied }: Props) {
  const fields = table.schema.fields;
  const rows = table.numRows;
  const cols = fields.length;
  const vectors = useMemo(() => fields.map((_, i) => table.getChildAt(i)!), [table, fields]);
  const text = useMemo(() => columnText(table.schema), [table]);
  const numeric = useMemo(() => fields.map((f) => isNumeric(f.type as DataType)), [fields]);
  // A new result mounts a new grid (its parent keys it), so these start fresh for each one.
  const [widths, setWidths] = useState<number[]>(() => initialWidths(table, columnText(table.schema)));
  const [scrollTop, setScrollTop] = useState(0);
  const [viewHeight, setViewHeight] = useState(400);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [viewing, setViewing] = useState<{ name: string; value: string } | null>(null);
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    // The observer reports the size it starts with too.
    const observer = new ResizeObserver(() => setViewHeight(el.clientHeight));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const first = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
  const last = Math.min(rows - 1, Math.ceil((scrollTop + viewHeight) / rowHeight) + overscan);
  const sel = selection ? bounds(selection) : null;
  const totalWidth = numberColumnWidth + widths.reduce((a, b) => a + b, 0);

  const cellText = useCallback((r: number, c: number) => text[c]!(vectors[c]!.get(r)), [text, vectors]);

  const copySelection = useCallback(async () => {
    if (!selection) return;
    const b = bounds(selection);
    const tsv = toDelimited(table, "\t", { header: false, rows: [b.r0, b.r1], columns: [b.c0, b.c1] });
    const ok = await copyText(tsv.replace(/\n$/, ""));
    onCopied?.(ok ? (b.r1 - b.r0 + 1) * (b.c1 - b.c0 + 1) : false);
  }, [selection, table, onCopied]);

  const scrollIntoView = (r: number) => {
    const el = scroller.current;
    if (!el) return;
    const top = r * rowHeight;
    const headerH = rowHeight;
    if (top < el.scrollTop) el.scrollTop = top;
    else if (top + rowHeight > el.scrollTop + el.clientHeight - headerH) {
      el.scrollTop = top + rowHeight - el.clientHeight + headerH;
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return; // keys typed in the value dialog
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.key.toLowerCase() === "c") {
      if (!selection) return;
      e.preventDefault();
      void copySelection();
      return;
    }
    if (mod && e.key.toLowerCase() === "a") {
      e.preventDefault();
      if (rows > 0 && cols > 0) setSelection({ anchor: [0, 0], focus: [rows - 1, cols - 1] });
      return;
    }
    const moves: Record<string, Cell> = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] };
    const move = moves[e.key];
    if (move && rows > 0 && cols > 0) {
      e.preventDefault();
      const from = selection?.focus ?? [0, 0];
      const to: Cell = [
        Math.max(0, Math.min(rows - 1, from[0] + move[0])),
        Math.max(0, Math.min(cols - 1, from[1] + move[1])),
      ];
      setSelection(e.shiftKey && selection ? { anchor: selection.anchor, focus: to } : { anchor: to, focus: to });
      scrollIntoView(to[0]);
    }
    if (e.key === "Escape") setSelection(null);
  };

  const pick = (cell: Cell, extend: boolean) =>
    setSelection((s) => (extend && s ? { anchor: s.anchor, focus: cell } : { anchor: cell, focus: cell }));

  const startColumnResize = (e: PointerEvent, c: number) => {
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const start = widths[c]!;
    const move = (ev: globalThis.PointerEvent) =>
      setWidths((w) => w.map((x, i) => (i === c ? Math.max(48, start + ev.clientX - startX) : x)));
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  const body = [];
  for (let r = first; r <= last; r++) {
    const rowSelected = sel && r >= sel.r0 && r <= sel.r1;
    const cells = [];
    for (let c = 0; c < cols; c++) {
      const t = cellText(r, c);
      const selected = rowSelected && c >= sel.c0 && c <= sel.c1;
      cells.push(
        <div
          key={c}
          role="gridcell"
          aria-colindex={c + 2}
          aria-selected={selected || undefined}
          className={`grid-cell${numeric[c] ? " grid-num" : ""}${t === null ? " grid-null" : ""}${selected ? " grid-selected" : ""}`}
          style={{ width: widths[c] }}
          title={t !== null && t.length > 24 ? t.slice(0, 2000) : undefined}
          onPointerDown={(e) => {
            if (e.button !== 0) return;
            pick([r, c], e.shiftKey);
          }}
          onDoubleClick={() => setViewing({ name: fields[c]!.name, value: t ?? "NULL" })}
        >
          {t ?? "NULL"}
        </div>,
      );
    }
    body.push(
      <div
        key={r}
        role="row"
        aria-rowindex={r + 2}
        className={`grid-row${r % 2 ? " grid-row-odd" : ""}`}
        style={{ transform: `translateY(${(r + 1) * rowHeight}px)`, width: totalWidth }}
      >
        <div
          role="rowheader"
          className={`grid-cell grid-rownum${rowSelected && sel.c0 === 0 && sel.c1 === cols - 1 ? " grid-selected" : ""}`}
          style={{ width: numberColumnWidth }}
          onPointerDown={(e) =>
            setSelection((s) =>
              e.shiftKey && s
                ? { anchor: [s.anchor[0], 0], focus: [r, cols - 1] }
                : { anchor: [r, 0], focus: [r, cols - 1] },
            )
          }
        >
          {r + 1}
        </div>
        {cells}
      </div>,
    );
  }

  return (
    <div
      ref={scroller}
      className="grid"
      role="grid"
      aria-label="Results"
      aria-rowcount={rows + 1}
      aria-colcount={cols + 1}
      tabIndex={0}
      onKeyDown={onKeyDown}
      onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
      data-testid="results-grid"
    >
      <div className="grid-canvas" style={{ height: (rows + 1) * rowHeight, width: totalWidth }}>
        <div role="row" aria-rowindex={1} className="grid-row grid-header" style={{ width: totalWidth }}>
          <div
            role="columnheader"
            className="grid-cell grid-rownum grid-corner"
            style={{ width: numberColumnWidth }}
            title="Select all"
            onPointerDown={() => rows > 0 && setSelection({ anchor: [0, 0], focus: [rows - 1, cols - 1] })}
          >
            #
          </div>
          {fields.map((f, c) => (
            <div
              key={c}
              role="columnheader"
              aria-colindex={c + 2}
              className={`grid-cell grid-head${numeric[c] ? " grid-num" : ""}`}
              style={{ width: widths[c] }}
              title={`${f.name}: ${typeLabel(f)}`}
              onPointerDown={(e) => {
                if (e.button !== 0 || rows === 0) return;
                setSelection((s) =>
                  e.shiftKey && s
                    ? { anchor: [0, s.anchor[1]], focus: [rows - 1, c] }
                    : { anchor: [0, c], focus: [rows - 1, c] },
                );
              }}
            >
              <span className="grid-head-name">{f.name}</span>
              <span className="grid-head-type">{typeLabel(f)}</span>
              <span
                className="grid-resize"
                role="separator"
                aria-label={`Resize ${f.name}`}
                onPointerDown={(e) => startColumnResize(e, c)}
              />
            </div>
          ))}
        </div>
        {body}
      </div>
      {rows === 0 && <div className="grid-empty">No rows</div>}
      {viewing && <ValueDialog name={viewing.name} value={viewing.value} onClose={() => setViewing(null)} />}
    </div>
  );
}

function initialWidths(table: Table, text: ((v: unknown) => string | null)[]): number[] {
  const sample = Math.min(table.numRows, 50);
  return table.schema.fields.map((f, c) => {
    const v = table.getChildAt(c)!;
    // The header holds the name and the type; cells are monospaced (about 7.5 px a character).
    const header = f.name.length * 7.6 + typeLabel(f).length * 6.4 + 28;
    let longest = 0;
    for (let r = 0; r < sample; r++) longest = Math.max(longest, (text[c]!(v.get(r)) ?? "NULL").length);
    return Math.round(Math.max(64, Math.min(360, Math.max(header, longest * 7.6 + 20))));
  });
}
