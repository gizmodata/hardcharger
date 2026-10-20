// Excel workbooks (.xlsx, .xls, .xlsb) through SheetJS Community Edition, which reads the file
// itself: no formula is calculated (a formula's value is the one Excel saved with it), no
// macro runs, nothing is fetched. A sheet becomes rows of cells: numbers, text, booleans, and
// dates and times where the cell's number format is a date format, computed with the
// workbook's own date system (1900 or 1904). Error cells (#N/A, #DIV/0!, …) are empty. A
// merged range keeps its value in its first cell; the others are empty, as Excel stores them.
//
// The workbook is read whole, in the upload worker, so a large or malformed file can't freeze
// the page; the caller caps the file's size first.

import type { CellObject, WorkBook, WorkSheet } from "xlsx";

import type { Cell, SheetMoment } from "./types";
import { ValueError, serialToMs } from "./values";

export class ExcelError extends Error {}

/** Excel's own limits: a cell holds at most 32,767 characters; a sheet 1,048,576 rows. */
export const maxCellChars = 32_767;
export const maxSheetRows = 1_048_576;

type Xlsx = typeof import("xlsx");

let library: Promise<Xlsx> | undefined;

/** SheetJS, loaded the first time a workbook is read (a chunk of its own). */
export function loadXlsx(): Promise<Xlsx> {
  library ??= import("xlsx");
  return library;
}

export interface SheetGrid {
  sheet: string;
  /** Every non-empty row, its cells from the first column with any content. */
  rows: Cell[][];
  /** Each row's number in the sheet (1-based), for messages. */
  rowNumbers: number[];
  /** The sheet column of the grid's first column (0 = A). */
  firstColumn: number;
  width: number;
  epoch1904: boolean;
  notes: string[];
}

/** The column letters of a 0-based index: 0 → A, 26 → AA. */
export function columnLetters(index: number): string {
  let n = index + 1;
  let out = "";
  while (n > 0) {
    const r = (n - 1) % 26;
    out = String.fromCharCode(65 + r) + out;
    n = Math.floor((n - 1) / 26);
  }
  return out;
}

/** What a date number format shows: a date, a time of day, or both. */
export function formatShows(format: string): SheetMoment["shows"] {
  // Leave out quoted text, escaped characters and [colour]/[$-locale] sections.
  const f = format
    .replace(/"[^"]*"/g, "")
    .replace(/\\./g, "")
    .replace(/\[(?!h\]|m\]|s\])[^\]]*\]/gi, "")
    .toLowerCase();
  const time = /[hs]|am\/pm|a\/p/.test(f);
  const date = /[dy]/.test(f) || (/m/.test(f) && !time);
  if (date && !time) return "date";
  if (time && !date) return "time";
  return "datetime";
}

/** The sheet names of a workbook, in order (read without their cells). */
export async function sheetNames(bytes: Uint8Array): Promise<string[]> {
  const XLSX = await loadXlsx();
  let wb: WorkBook;
  try {
    wb = XLSX.read(bytes, { type: "array", bookSheets: true, WTF: false });
  } catch (err) {
    throw new ExcelError(`The file can't be read as an Excel workbook: ${(err as Error).message}`);
  }
  return wb.SheetNames;
}

/** One sheet's cells (the first sheet when `sheet` is empty). */
export async function readSheet(bytes: Uint8Array, sheet: string): Promise<SheetGrid> {
  const XLSX = await loadXlsx();
  let wb: WorkBook;
  try {
    wb = XLSX.read(bytes, {
      type: "array",
      dense: true,
      cellDates: false,
      cellNF: true,
      cellText: false,
      cellHTML: false,
      cellFormula: false,
      sheetRows: maxSheetRows,
      ...(sheet ? { sheets: [sheet] } : { sheets: 0 }),
      WTF: false,
    });
  } catch (err) {
    throw new ExcelError(`The file can't be read as an Excel workbook: ${(err as Error).message}`);
  }
  const name = sheet || wb.SheetNames[0];
  if (name === undefined) throw new ExcelError("The workbook has no sheets.");
  const ws: WorkSheet | undefined = wb.Sheets[name];
  if (!ws) throw new ExcelError(`The workbook has no sheet named "${sheet}".`);
  const epoch1904 = Boolean(wb.Workbook?.WBProps?.date1904);
  const ssf = XLSX.SSF as { is_date: (format: string) => boolean };
  const isDate = (fmt: string | undefined) => fmt !== undefined && ssf.is_date(fmt);
  return gridOf(ws, name, epoch1904, isDate);
}

function gridOf(ws: WorkSheet, name: string, epoch1904: boolean, isDate: (fmt: string | undefined) => boolean) {
  const notes: string[] = [];
  const dense = (ws as { "!data"?: (CellObject[] | undefined)[] })["!data"] ?? [];
  let errors = 0;
  let unsaved = 0;
  let badDates = 0;
  let firstColumn = Number.POSITIVE_INFINITY;
  let lastColumn = -1;
  const raw: { row: number; cells: Map<number, Cell> }[] = [];
  for (let r = 0; r < dense.length; r++) {
    const line = dense[r];
    if (!line) continue;
    const cells = new Map<number, Cell>();
    for (let c = 0; c < line.length; c++) {
      const cell = line[c];
      if (!cell) continue;
      let value: Cell = null;
      switch (cell.t) {
        case "s":
        case "str" as CellObject["t"]: {
          const s = typeof cell.v === "string" ? cell.v : String(cell.v ?? "");
          if (s.length > maxCellChars) {
            throw new ExcelError(
              `Cell ${columnLetters(c)}${r + 1} holds more than ${maxCellChars.toLocaleString()} characters, ` +
                "more than Excel allows: the file isn't a workbook Excel wrote.",
            );
          }
          value = s === "" ? null : s;
          break;
        }
        case "b":
          value = Boolean(cell.v);
          break;
        case "n": {
          if (typeof cell.v !== "number") {
            if (cell.f !== undefined) unsaved++;
            break;
          }
          if (isDate(cell.z as string | undefined)) {
            try {
              value = { ms: serialToMs(cell.v, epoch1904), shows: formatShows(cell.z as string) };
            } catch (err) {
              if (!(err instanceof ValueError)) throw err;
              badDates++;
            }
          } else {
            value = cell.v;
          }
          break;
        }
        case "d": {
          const v = cell.v as unknown;
          if (v instanceof Date) value = { ms: v.getTime(), shows: "datetime" };
          else if (typeof v === "number") value = { ms: serialToMs(v, epoch1904), shows: "datetime" };
          break;
        }
        case "e":
          errors++;
          break;
        default:
          if (cell.f !== undefined && cell.v === undefined) unsaved++;
      }
      if (value === null) continue;
      cells.set(c, value);
      firstColumn = Math.min(firstColumn, c);
      lastColumn = Math.max(lastColumn, c);
    }
    if (cells.size > 0) raw.push({ row: r + 1, cells });
  }
  const width = lastColumn < 0 ? 0 : lastColumn - firstColumn + 1;
  const rows: Cell[][] = [];
  const rowNumbers: number[] = [];
  for (const { row, cells } of raw) {
    const out = new Array<Cell>(width).fill(null);
    for (const [c, v] of cells) out[c - firstColumn] = v;
    rows.push(out);
    rowNumbers.push(row);
  }
  const span = raw.length > 0 ? raw[raw.length - 1]!.row - raw[0]!.row + 1 : 0;
  const skipped = span - raw.length;
  if (skipped > 0) notes.push(`${plural(skipped, "empty row")} left out.`);
  if (errors > 0) notes.push(`${plural(errors, "error cell")} (#N/A, #DIV/0!, …) loaded as NULL.`);
  if (unsaved > 0) {
    notes.push(`${plural(unsaved, "formula")} with no saved value loaded as NULL (open and save the file in Excel).`);
  }
  if (badDates > 0) notes.push(`${plural(badDates, "date")} Excel can't show (1900-02-29) loaded as NULL.`);
  const merges = ws["!merges"]?.length ?? 0;
  if (merges > 0) {
    notes.push(`${plural(merges, "merged range")}: the value is in each range's first cell; the others are NULL.`);
  }
  if (epoch1904) notes.push("The workbook counts dates from 1904 (its dates are read that way).");
  return {
    sheet: name,
    rows,
    rowNumbers,
    firstColumn: Number.isFinite(firstColumn) ? firstColumn : 0,
    width,
    epoch1904,
    notes,
  };
}

function plural(n: number, word: string): string {
  return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
}
