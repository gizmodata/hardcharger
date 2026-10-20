// Type inference, header detection and column names, from the first rows of a file.
//
// Text (CSV) infers as DuckDB's CSV sniffer would, with one difference: a whole number with a
// leading zero (`00123`, a ZIP code or an id) stays VARCHAR so it keeps its zeros. JSON
// strings are dates and times when they look like ones; a spreadsheet's text stays text (a
// "number stored as text" keeps its form) and its numbers, booleans and dates keep theirs.

import { type Cell, type ColumnType, isJsonText, isMoment } from "./types";
import { ValueError, cellText, parseBigint, parseDate, parseDouble, parseTime, parseTimestamp } from "./values";

/** How much of a string a reader's inference looks into. */
export type TextInference = "full" | "temporal" | "none";

/** Rows the inference reads (DuckDB's CSV sniffer reads as many by default). */
export const inferenceRows = 20_480;

const leadingZero = /^[+-]?0\d/;
const boolText = /^(true|false)$/i;

type Kind = "bool" | "int" | "double" | "date" | "time" | "timestamp" | "timestamptz" | "text";

function fits(parse: (t: string) => unknown, text: string): boolean {
  try {
    parse(text);
    return true;
  } catch (err) {
    if (err instanceof ValueError) return false;
    throw err;
  }
}

/** What one non-empty text value looks like. */
function textKind(text: string, mode: TextInference): Kind {
  const t = text.trim();
  if (mode === "full") {
    if (boolText.test(t)) return "bool";
    if (!leadingZero.test(t) && fits(parseBigint, t)) return "int";
    if (!leadingZero.test(t) && /\d/.test(t) && fits(parseDouble, t)) return "double";
  }
  if (mode === "none") return "text";
  if (fits(parseDate, t)) return "date";
  if (fits(parseTime, t)) return "time";
  try {
    return parseTimestamp(t).zoned ? "timestamptz" : "timestamp";
  } catch (err) {
    if (err instanceof ValueError) return "text";
    throw err;
  }
}

function cellKind(cell: Exclude<Cell, null>, mode: TextInference): Kind {
  if (typeof cell === "string") return textKind(cell, mode);
  if (typeof cell === "boolean") return "bool";
  if (typeof cell === "number") return Number.isSafeInteger(cell) ? "int" : "double";
  if (isMoment(cell)) return cell.shows === "date" ? "date" : cell.shows === "time" ? "time" : "timestamp";
  if (isJsonText(cell)) return "text";
  return "text";
}

/** The kind that holds both `a` and `b`. */
function widen(a: Kind, b: Kind): Kind {
  if (a === b) return a;
  const pair = new Set([a, b]);
  if (pair.has("int") && pair.has("double")) return "double";
  if (pair.has("date") && (pair.has("timestamp") || pair.has("timestamptz"))) {
    return pair.has("timestamptz") ? "timestamptz" : "timestamp";
  }
  if (pair.has("timestamp") && pair.has("timestamptz")) return "timestamptz";
  return "text";
}

const kindType: Record<Kind, ColumnType> = {
  bool: { kind: "BOOLEAN" },
  int: { kind: "BIGINT" },
  double: { kind: "DOUBLE" },
  date: { kind: "DATE" },
  time: { kind: "TIME" },
  timestamp: { kind: "TIMESTAMP" },
  timestamptz: { kind: "TIMESTAMPTZ" },
  text: { kind: "VARCHAR" },
};

/** The type of a column from its values (null and empty text are ignored; none at all: VARCHAR). */
export function inferType(values: Iterable<Cell>, mode: TextInference): ColumnType {
  let kind: Kind | undefined;
  for (const v of values) {
    if (v === null || v === "") continue;
    const k = cellKind(v, mode);
    kind = kind === undefined ? k : widen(kind, k);
    if (kind === "text") break;
  }
  return kindType[kind ?? "text"];
}

/** The types of every column of `rows` (rows may be shorter than `width`). */
export function inferTypes(rows: readonly (readonly Cell[])[], width: number, mode: TextInference): ColumnType[] {
  const out: ColumnType[] = [];
  for (let c = 0; c < width; c++) out.push(inferType(column(rows, c), mode));
  return out;
}

function* column(rows: readonly (readonly Cell[])[], c: number): Generator<Cell> {
  for (const r of rows) yield r[c] ?? null;
}

export type HeaderChoice = "auto" | "yes" | "no";

/**
 * Whether the first row holds column names. Detect: every cell of it is text that isn't a
 * number, date or boolean, and the names are distinct; and either some column below holds
 * something other than text, or every name is filled in (an all-text table with a full first
 * row, which is nearly always a header).
 */
export function detectHeader(rows: readonly (readonly Cell[])[], width: number, mode: TextInference): boolean {
  const first = rows[0];
  if (!first) return false;
  const names: string[] = [];
  let filled = 0;
  for (let c = 0; c < width; c++) {
    const v = first[c] ?? null;
    if (v === null || v === "") continue;
    if (typeof v !== "string") return false;
    if (cellKind(v, mode === "none" ? "full" : mode) !== "text") return false;
    names.push(v.trim().toLowerCase());
    filled++;
  }
  if (filled === 0 || new Set(names).size !== names.length) return false;
  const rest = rows.slice(1);
  if (rest.length === 0) return true;
  const below = inferTypes(rest, width, mode);
  if (below.some((t) => t.kind !== "VARCHAR")) return true;
  return filled === width;
}

/**
 * Column names: the header's (trimmed), else column0, column1, … as DuckDB names them; an
 * empty name becomes column<i>, and a repeated one (in any case) gets _1, _2, ….
 */
export function columnNames(header: readonly Cell[] | undefined, width: number): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (let c = 0; c < width; c++) {
    const raw = header?.[c];
    let name = raw === null || raw === undefined ? "" : typeof raw === "string" ? raw.trim() : cellText(raw);
    if (name === "") name = `column${c}`;
    let candidate = name;
    for (let n = 1; seen.has(candidate.toLowerCase()); n++) candidate = `${name}_${n}`;
    seen.add(candidate.toLowerCase());
    out.push(candidate);
  }
  return out;
}
