// Reading an upload's file: what it holds (inspect: its columns, their types, the first rows),
// and its rows as Arrow record batches of a few megabytes each (convert), read from the file
// as they're needed, so a large CSV, JSON-lines, Parquet or Arrow stream is uploaded in
// bounded memory. Runs in the upload worker (upload.worker.ts).

import {
  type Data,
  type DataType,
  type Field,
  RecordBatch,
  RecordBatchReader,
  Schema,
  Utf8,
  makeData,
  Field as ArrowField,
  Struct,
} from "apache-arrow";

import type { ParquetFile } from "parquet-wasm/esm";

import { arrowTypeLabel, textFor } from "../format";
import { ColumnBuffer, outputSchema, recordBatch } from "./arrow";
import { CsvError, CsvParser, type CsvRecord, sniffDelimiter } from "./csv";
import { ExcelError, type SheetGrid, columnLetters, readSheet, sheetNames } from "./excel";
import {
  type UploadFormat,
  formatBytes,
  formatFromBytes,
  formatFromName,
  isDelimited,
  isExcel,
  wholeFileLimit,
} from "./formats";
import { type HeaderChoice, columnNames, detectHeader, inferTypes, inferenceRows, type TextInference } from "./infer";
import { JsonColumns, JsonError, LineSplitter, parseJson } from "./json";
import { type Cell, type ColumnType, type OutputColumn, type SourceColumn, columnTypeOf } from "./types";
import { type ConvertContext, ValueError, cellText, converter } from "./values";

/** A problem with the file or the upload, in words for the user. */
export class UploadError extends Error {}

/** A line of JSON lines that isn't JSON. */
class JsonSyntaxError extends UploadError {
  readonly line: number;
  constructor(message: string, line: number) {
    super(message);
    this.line = line;
  }
}

export interface ReadOptions {
  header: HeaderChoice;
  /** Empty: detect (CSV). */
  delimiter: string;
  /** Empty: the first sheet (Excel). */
  sheet: string;
}

/** How the file is read, as inspect settled it (convert reads it the same way). */
export interface Settings {
  format: UploadFormat;
  header: boolean;
  delimiter: string;
  encoding: string;
  sheet: string;
  jsonArray: boolean;
  arrowFile: boolean;
  /** JSON: the columns, in order. */
  keys: string[];
}

export interface Inspection {
  settings: Settings;
  columns: SourceColumn[];
  /** The first rows (100 at most), after the header row. */
  preview: Cell[][];
  /** Excel: the workbook's sheets. */
  sheets?: string[];
  /** Rows in the file, when known before reading them all. */
  rows?: number;
  context: ConvertContext;
  notes: string[];
}

export interface Chunk {
  batch: RecordBatch;
  /** How far through the file (0 to 1). */
  fraction: number;
}

export const previewRows = 100;
/** A batch closes at about this many bytes (one ingest request each). */
export const chunkBytes = 8 << 20;
const chunkRows = 1_000_000;
const readBytes = 4 << 20;
const sampleBytes = 1 << 20;

// ── Text ─────────────────────────────────────────────────────────────────────────────────

/** The file's text encoding: a byte-order mark's, else UTF-8 if the start of it is, else Windows-1252. */
function sniffEncoding(head: Uint8Array): { encoding: string; note?: string } {
  if (head[0] === 0xef && head[1] === 0xbb && head[2] === 0xbf) return { encoding: "utf-8" };
  if (head[0] === 0xff && head[1] === 0xfe) return { encoding: "utf-16le" };
  if (head[0] === 0xfe && head[1] === 0xff) return { encoding: "utf-16be" };
  try {
    new TextDecoder("utf-8", { fatal: true }).decode(head, { stream: true });
    return { encoding: "utf-8" };
  } catch {
    return {
      encoding: "windows-1252",
      note:
        "The file isn't UTF-8: it was read as Windows-1252 (Western European). If its accented letters look " +
        "wrong, save it as UTF-8 (in Excel: CSV UTF-8) and upload it again.",
    };
  }
}

async function head(file: Blob, bytes: number): Promise<Uint8Array> {
  return new Uint8Array(await file.slice(0, bytes).arrayBuffer());
}

/** The file's text in chunks, with how many of its bytes each one ends at. */
async function* textOf(file: Blob, encoding: string): AsyncGenerator<{ text: string; at: number }> {
  const decoder = new TextDecoder(encoding, { fatal: encoding === "utf-8", ignoreBOM: false });
  const reader = file.stream().getReader();
  let at = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      // A stream's chunks are small: put them together into reads of a few megabytes.
      let bytes = value;
      if (bytes.length < readBytes) {
        const parts = [bytes];
        let size = bytes.length;
        while (size < readBytes) {
          const more = await reader.read();
          if (more.done) break;
          parts.push(more.value);
          size += more.value.length;
        }
        if (parts.length > 1) {
          bytes = new Uint8Array(size);
          let o = 0;
          for (const p of parts) {
            bytes.set(p, o);
            o += p.length;
          }
        }
      }
      let text: string;
      try {
        text = decoder.decode(bytes, { stream: true });
      } catch {
        throw new UploadError(
          `The file isn't valid UTF-8 (after about ${formatBytes(at)}): save it as UTF-8 (in Excel: CSV UTF-8) and ` +
            "upload it again.",
        );
      }
      at += bytes.length;
      if (text) yield { text, at };
    }
    const rest = decoder.decode();
    if (rest) yield { text: rest, at };
  } finally {
    // An early stop (the inspection read enough) lets go of the file.
    void reader.cancel().catch(() => {});
  }
}

// ── Rows of cells (CSV, Excel, JSON) ─────────────────────────────────────────────────────

interface Row {
  cells: Cell[];
  /** Where the row is, for messages: "Line 12", "Row 12". */
  where: string;
  /** Excel: the sheet row, to name a cell (C12). */
  sheetRow?: number;
}

interface RowSource {
  rows: AsyncGenerator<Row>;
  fraction: () => number;
}

function csvRows(file: Blob, settings: Settings, width: number | undefined): RowSource {
  let at = 0;
  const size = Math.max(file.size, 1);
  async function* rows(): AsyncGenerator<Row> {
    const pending: { fields: CsvRecord; line: number }[] = [];
    const parser = new CsvParser(settings.delimiter, (fields, line) => pending.push({ fields, line }));
    let first = true;
    let expected = width;
    const take = function* () {
      for (const { fields, line } of pending.splice(0)) {
        if (first) {
          first = false;
          expected ??= fields.length;
          if (settings.header) continue;
        }
        if (fields.length !== expected) {
          throw new UploadError(
            `Line ${line.toLocaleString()} has ${fields.length.toLocaleString()} field${fields.length === 1 ? "" : "s"}, ` +
              `but the file's first line has ${expected!.toLocaleString()} (delimiter "${settings.delimiter === "\t" ? "tab" : settings.delimiter}"). ` +
              "Check the delimiter and the quotes.",
          );
        }
        yield { cells: fields, where: `Line ${line.toLocaleString()}` };
      }
    };
    try {
      for await (const chunk of textOf(file, settings.encoding)) {
        parser.push(at === 0 ? chunk.text.replace(/^\ufeff/, "") : chunk.text);
        at = chunk.at;
        yield* take();
      }
      parser.end();
      yield* take();
    } catch (err) {
      if (err instanceof CsvError) throw new UploadError(err.message);
      throw err;
    }
  }
  return { rows: rows(), fraction: () => at / size };
}

function gridRows(grid: SheetGrid, header: boolean): RowSource {
  let done = 0;
  const total = Math.max(grid.rows.length - (header ? 1 : 0), 1);
  async function* rows(): AsyncGenerator<Row> {
    for (let i = header ? 1 : 0; i < grid.rows.length; i++) {
      done++;
      const row = grid.rowNumbers[i]!;
      yield { cells: grid.rows[i]!, where: `Row ${row.toLocaleString()}`, sheetRow: row };
    }
    await Promise.resolve();
  }
  return { rows: rows(), fraction: () => done / total };
}

function jsonLinesRows(file: Blob, settings: Settings, columns: JsonColumns): RowSource {
  let at = 0;
  const size = Math.max(file.size, 1);
  async function* rows(): AsyncGenerator<Row> {
    const pending: Row[] = [];
    const lines = new LineSplitter((line, number) => {
      if (line.trim() === "") return;
      const where = `Line ${number.toLocaleString()}`;
      let value: unknown;
      try {
        value = parseJson(line);
      } catch (err) {
        throw new JsonSyntaxError(`${where} isn't valid JSON: ${(err as Error).message}`, number);
      }
      pending.push({ cells: columns.row(value, where), where });
    });
    for await (const chunk of textOf(file, settings.encoding)) {
      lines.push(at === 0 ? chunk.text.replace(/^\ufeff/, "") : chunk.text);
      at = chunk.at;
      yield* pending.splice(0);
    }
    lines.end();
    yield* pending.splice(0);
  }
  return { rows: wrapJson(rows()), fraction: () => at / size };
}

async function* wrapJson(rows: AsyncGenerator<Row>): AsyncGenerator<Row> {
  try {
    yield* rows;
  } catch (err) {
    if (err instanceof JsonError) throw new UploadError(err.message);
    throw err;
  }
}

async function jsonArray(file: Blob, settings: Settings): Promise<unknown[]> {
  let text = "";
  for await (const chunk of textOf(file, settings.encoding)) text += chunk.text;
  let value: unknown;
  try {
    value = parseJson(text.replace(/^\ufeff/, ""));
  } catch (err) {
    throw new UploadError(`The file isn't valid JSON: ${(err as Error).message}`);
  }
  return Array.isArray(value) ? (value as unknown[]) : [value];
}

function arrayRows(items: unknown[], columns: JsonColumns): RowSource {
  let done = 0;
  async function* rows(): AsyncGenerator<Row> {
    for (let i = 0; i < items.length; i++) {
      done = i + 1;
      const where = `Item ${(i + 1).toLocaleString()}`;
      yield { cells: columns.row(items[i], where), where };
    }
    await Promise.resolve();
  }
  return { rows: wrapJson(rows()), fraction: () => done / Math.max(items.length, 1) };
}

// ── Typed files (Parquet, Arrow) ──────────────────────────────────────────────────────────

interface TypedSource {
  schema: Schema;
  batches: AsyncGenerator<RecordBatch>;
  fraction: () => number;
  rows?: number;
}

let parquetReady: Promise<typeof import("parquet-wasm/esm")> | undefined;

async function parquetModule(): Promise<typeof import("parquet-wasm/esm")> {
  parquetReady ??= (async () => {
    const [mod, url] = await Promise.all([
      import("parquet-wasm/esm"),
      import("parquet-wasm/esm/parquet_wasm_bg.wasm?url"),
    ]);
    await mod.default({ module_or_path: url.default });
    return mod;
  })();
  parquetReady.catch(() => (parquetReady = undefined));
  return parquetReady;
}

function wasmToArrow(ipc: Uint8Array): RecordBatch[] {
  const reader = RecordBatchReader.from(ipc);
  return [...reader];
}

async function parquetSource(file: File, options: { limit?: number; batchRows?: number } = {}): Promise<TypedSource> {
  const pq = await parquetModule();
  let pf: ParquetFile;
  try {
    pf = await pq.ParquetFile.fromFile(file);
  } catch (err) {
    throw new UploadError(`The file can't be read as Parquet: ${String(err)}`);
  }
  const rows = pf.metadata().fileMetadata().numRows();
  const schema = RecordBatchReader.from(pf.schema().intoIPCStream()).open().schema;
  let done = 0;
  async function* batches(): AsyncGenerator<RecordBatch> {
    const stream = (await pf.stream({
      batchSize: options.batchRows ?? 65_536,
      ...(options.limit !== undefined ? { limit: options.limit } : {}),
    })) as ReadableStream<{ intoIPCStream: () => Uint8Array }>;
    const reader = stream.getReader();
    try {
      for (;;) {
        const { done: end, value } = await reader.read();
        if (end) break;
        for (const b of wasmToArrow(value.intoIPCStream())) {
          done += b.numRows;
          yield b;
        }
      }
    } finally {
      reader.releaseLock();
    }
  }
  return { schema, batches: batches(), fraction: () => done / Math.max(rows, 1), rows };
}

async function isArrowFile(file: Blob): Promise<boolean> {
  const h = await head(file, 6);
  return new TextDecoder().decode(h) === "ARROW1";
}

async function arrowSource(file: File, settings: Settings): Promise<TypedSource> {
  let at = 0;
  const size = Math.max(file.size, 1);
  try {
    if (settings.arrowFile) {
      const reader = RecordBatchReader.from(new Uint8Array(await file.arrayBuffer()));
      const all = [...reader];
      let i = 0;
      async function* batches(): AsyncGenerator<RecordBatch> {
        for (const b of all) {
          i++;
          yield b;
        }
        await Promise.resolve();
      }
      return {
        schema: reader.schema,
        batches: batches(),
        fraction: () => i / Math.max(all.length, 1),
        rows: all.reduce((n, b) => n + b.numRows, 0),
      };
    }
    const counted = file.stream().pipeThrough(
      new TransformStream<Uint8Array, Uint8Array>({
        transform(chunk, controller) {
          at += chunk.length;
          controller.enqueue(chunk);
        },
      }),
    );
    const reader = await RecordBatchReader.from(counted);
    await reader.open();
    async function* batches(): AsyncGenerator<RecordBatch> {
      for await (const b of reader) yield b;
    }
    return { schema: reader.schema, batches: batches(), fraction: () => at / size };
  } catch (err) {
    throw new UploadError(`The file can't be read as Arrow IPC: ${(err as Error).message}`);
  }
}

function typedColumns(schema: Schema): SourceColumn[] {
  return schema.fields.map((f: Field) => {
    const label = arrowTypeLabel(f.type as DataType);
    return { name: f.name, inferred: columnTypeOf(label) ?? { kind: "VARCHAR" }, fixed: label };
  });
}

async function typedPreview(source: TypedSource): Promise<Cell[][]> {
  const text = source.schema.fields.map((f) => textFor(f.type as DataType));
  const out: Cell[][] = [];
  for await (const b of source.batches) {
    for (let r = 0; r < b.numRows && out.length < previewRows; r++) {
      out.push(text.map((t, c) => t(b.getChildAt(c)?.get(r))));
    }
    if (out.length >= previewRows) break;
  }
  await source.batches.return(undefined);
  return out;
}

// ── Inspect ───────────────────────────────────────────────────────────────────────────────

/** Excel workbooks read once: the sheet a preview read is what the upload sends. */
const sheets = new WeakMap<Blob, Map<string, Promise<SheetGrid>>>();

function sheetOf(file: Blob, sheet: string): Promise<SheetGrid> {
  let bySheet = sheets.get(file);
  if (!bySheet) {
    bySheet = new Map();
    sheets.set(file, bySheet);
  }
  let grid = bySheet.get(sheet);
  if (!grid) {
    grid = (async () => {
      try {
        return await readSheet(new Uint8Array(await file.arrayBuffer()), sheet);
      } catch (err) {
        if (err instanceof ExcelError) throw new UploadError(err.message);
        throw err;
      }
    })();
    grid.catch(() => bySheet.delete(sheet));
    bySheet.set(sheet, grid);
  }
  return grid;
}

/**
 * Whether a file's first bytes are a workbook's: a zip (.xlsx, .xlsb), an OLE compound file or
 * BIFF records (.xls), or the HTML or XML that some applications save as .xls.
 */
function looksLikeWorkbook(format: UploadFormat, h: Uint8Array): boolean {
  if (h[0] === 0x50 && h[1] === 0x4b && h[2] === 0x03 && h[3] === 0x04) return true; // an .xlsx named .xls opens too
  if (format !== "xls") return false;
  if (h[0] === 0xd0 && h[1] === 0xcf && h[2] === 0x11 && h[3] === 0xe0) return true;
  if (h[0] === 0x09 && (h[1] === 0x00 || h[1] === 0x02 || h[1] === 0x04 || h[1] === 0x08)) return true;
  const text = new TextDecoder()
    .decode(h)
    .replace(/^\ufeff/, "")
    .trimStart();
  return text.startsWith("<");
}

/** The format of a file: its name's, else its first bytes'. */
export async function formatOf(file: File): Promise<UploadFormat> {
  return formatFromName(file.name) ?? formatFromBytes(await head(file, 16));
}

function inferMode(format: UploadFormat): TextInference {
  if (isExcel(format)) return "none";
  if (format === "json") return "temporal";
  return "full";
}

function cellColumns(sample: Cell[][], width: number, format: UploadFormat, names: string[]): SourceColumn[] {
  const types = inferTypes(sample, width, inferMode(format));
  return names.map((name, i) => ({ name, inferred: types[i] ?? { kind: "VARCHAR" } }));
}

function tooLarge(file: File, limit: number, what: string): UploadError {
  return new UploadError(
    `${file.name} is ${formatBytes(file.size)}, over the ${formatBytes(limit)} limit for ${what}, which this browser ` +
      "reads whole. Save it as CSV or Parquet (read as they upload, at any size) and upload that.",
  );
}

/** What the file holds, read with `options` (detect where they say so). */
export async function inspect(file: File, options: ReadOptions): Promise<Inspection> {
  const format = await formatOf(file);
  const notes: string[] = [];
  const settings: Settings = {
    format,
    header: false,
    delimiter: "",
    encoding: "utf-8",
    sheet: "",
    jsonArray: false,
    arrowFile: false,
    keys: [],
  };

  if (isExcel(format)) {
    const limit = wholeFileLimit(format, false, false)!;
    if (file.size > limit) throw tooLarge(file, limit, "Excel workbooks");
    if (!looksLikeWorkbook(format, await head(file, 16))) {
      throw new UploadError(
        `${file.name} isn't an Excel workbook (its contents aren't what a .${format} file holds). ` +
          "If it is a CSV or other text file, give it that extension and upload it again.",
      );
    }
    let names: string[];
    try {
      names = await sheetNames(new Uint8Array(await file.arrayBuffer()));
    } catch (err) {
      if (err instanceof ExcelError) throw new UploadError(err.message);
      throw err;
    }
    const sheet = options.sheet && names.includes(options.sheet) ? options.sheet : (names[0] ?? "");
    const grid = await sheetOf(file, sheet);
    settings.sheet = grid.sheet;
    const sample = grid.rows.slice(0, inferenceRows + 1);
    const header = options.header === "auto" ? detectHeader(sample, grid.width, "none") : options.header === "yes";
    settings.header = header;
    const data = header ? sample.slice(1) : sample;
    const names2 = columnNames(header ? grid.rows[0] : undefined, grid.width);
    notes.push(...grid.notes);
    return {
      settings,
      columns: cellColumns(grid.rows.slice(header ? 1 : 0), grid.width, format, names2),
      preview: data.slice(0, previewRows),
      sheets: names,
      rows: grid.rows.length - (header ? 1 : 0),
      context: { serialDates: true, epoch1904: grid.epoch1904 },
      notes,
    };
  }

  if (format === "parquet") {
    const source = await parquetSource(file, { limit: previewRows });
    return {
      settings,
      columns: typedColumns(source.schema),
      preview: await typedPreview(source),
      rows: source.rows ?? 0,
      context: {},
      notes,
    };
  }

  if (format === "arrow") {
    settings.arrowFile = await isArrowFile(file);
    if (settings.arrowFile && file.size > (wholeFileLimit(format, false, true) ?? Infinity)) {
      throw tooLarge(file, wholeFileLimit(format, false, true)!, "Arrow IPC files (the .arrows stream format isn't)");
    }
    const source = await arrowSource(file, settings);
    return {
      settings,
      columns: typedColumns(source.schema),
      preview: await typedPreview(source),
      ...(source.rows !== undefined ? { rows: source.rows } : {}),
      context: {},
      notes,
    };
  }

  // Text: CSV, TSV, JSON.
  const start = await head(file, sampleBytes);
  const enc = sniffEncoding(start);
  settings.encoding = enc.encoding;
  if (enc.note) notes.push(enc.note);

  if (format === "json") {
    const firstText = new TextDecoder(enc.encoding).decode(start.subarray(0, 4096)).replace(/^\ufeff/, "");
    settings.jsonArray = firstText.trimStart().startsWith("[");
    const columns = new JsonColumns();
    const sample: Cell[][] = [];
    let source: RowSource;
    if (settings.jsonArray) {
      if (file.size > (wholeFileLimit(format, true, false) ?? Infinity)) {
        throw tooLarge(file, wholeFileLimit(format, true, false)!, "JSON arrays (JSON lines aren't)");
      }
      source = arrayRows(await jsonArray(file, settings), columns);
    } else {
      source = jsonLinesRows(file, settings, columns);
    }
    try {
      for await (const row of source.rows) {
        sample.push(row.cells);
        if (sample.length >= inferenceRows) break;
      }
    } catch (err) {
      // One JSON object over several lines: the whole file is one value.
      if (
        !settings.jsonArray &&
        err instanceof JsonSyntaxError &&
        err.line === 1 &&
        file.size <= (wholeFileLimit(format, true, false) ?? 0)
      ) {
        settings.jsonArray = true;
        const fallback = new JsonColumns();
        source = arrayRows(await jsonArray(file, settings), fallback);
        for await (const row of source.rows) {
          sample.push(row.cells);
          if (sample.length >= inferenceRows) break;
        }
        return jsonInspection(settings, fallback, sample, notes);
      }
      throw err;
    } finally {
      await source.rows.return(undefined);
    }
    return jsonInspection(settings, columns, sample, notes);
  }

  // CSV and TSV.
  const sampleText = new TextDecoder(enc.encoding).decode(start).replace(/^\ufeff/, "");
  const typed = options.delimiter ? options.delimiter : undefined;
  settings.delimiter = typed ?? sniffDelimiter(sampleText, format === "tsv");
  // The first records, for the header, the types and the preview.
  const records: CsvRecord[] = [];
  const firstPass: Settings = { ...settings, header: false };
  const source = csvRows(file, firstPass, undefined);
  try {
    for await (const row of source.rows) {
      records.push(row.cells as CsvRecord);
      if (records.length > inferenceRows) break;
    }
  } finally {
    await source.rows.return(undefined);
  }
  const width = records[0]?.length ?? 0;
  const header = options.header === "auto" ? detectHeader(records, width, "full") : options.header === "yes";
  settings.header = header;
  const data = header ? records.slice(1) : records;
  const names = columnNames(header ? records[0] : undefined, width);
  return {
    settings,
    columns: cellColumns(data, width, format, names),
    preview: data.slice(0, previewRows),
    context: {},
    notes,
  };
}

function jsonInspection(settings: Settings, columns: JsonColumns, sample: Cell[][], notes: string[]): Inspection {
  const width = columns.names.length;
  const rows = sample.map((r) => (r.length < width ? [...r, ...new Array<Cell>(width - r.length).fill(null)] : r));
  settings.keys = [...columns.names];
  return {
    settings,
    columns: cellColumns(rows, width, "json", columnNames(columns.names, width)),
    preview: rows.slice(0, previewRows),
    context: {},
    notes,
  };
}

// ── Convert ───────────────────────────────────────────────────────────────────────────────

function cellName(row: Row, column: number, grid: SheetGrid | undefined): string {
  if (grid && row.sheetRow !== undefined) return `Cell ${columnLetters(grid.firstColumn + column)}${row.sheetRow}`;
  return row.where;
}

function shown(cell: Cell): string {
  if (cell === null) return "NULL";
  const t = cellText(cell);
  return JSON.stringify(t.length > 60 ? `${t.slice(0, 57)}…` : t);
}

/** The file's rows as record batches of `columns` (converted, renamed, reordered). */
export async function* convert(
  file: File,
  settings: Settings,
  columns: readonly OutputColumn[],
  context: ConvertContext,
): AsyncGenerator<Chunk> {
  const format = settings.format;
  if (format === "parquet" || format === "arrow") {
    yield* convertTyped(file, settings, columns);
    return;
  }
  let grid: SheetGrid | undefined;
  let source: RowSource;
  if (isExcel(format)) {
    grid = await sheetOf(file, settings.sheet);
    source = gridRows(grid, settings.header);
  } else if (format === "json") {
    const keys = new JsonColumns();
    for (const k of settings.keys) keys.row({ [k]: null }, "");
    keys.open = false;
    source = settings.jsonArray
      ? arrayRows(await jsonArray(file, settings), keys)
      : jsonLinesRows(file, settings, keys);
  } else if (isDelimited(format)) {
    source = csvRows(file, settings, undefined);
  } else {
    throw new UploadError(`Unsupported format ${format}.`);
  }
  const schema = outputSchema(columns);
  const convs = columns.map((c) => {
    const conv = converter(c.type, context);
    const blankIsNull = c.type.kind !== "VARCHAR";
    return (cell: Cell) => conv(blankIsNull && typeof cell === "string" && cell.trim() === "" ? null : cell);
  });
  const buffers = columns.map((c) => new ColumnBuffer(c.type));
  let rows = 0;
  const flush = (): RecordBatch => {
    const children = buffers.map((b) => b.finish());
    const batch = recordBatch(schema, children, rows);
    rows = 0;
    return batch;
  };
  for await (const row of source.rows) {
    let bytes = 0;
    for (let i = 0; i < columns.length; i++) {
      const col = columns[i]!;
      const cell = col.source === null ? null : (row.cells[col.source] ?? null);
      try {
        buffers[i]!.push(convs[i]!(cell));
      } catch (err) {
        if (!(err instanceof ValueError)) throw err;
        throw new UploadError(
          `${cellName(row, col.source ?? 0, grid)}, column "${col.name}": ${shown(cell)} is ${err.message}. ` +
            "Change the column's type under Preview & column types (VARCHAR keeps any text), or fix the file.",
        );
      }
      bytes += buffers[i]!.bytes;
    }
    rows++;
    if (bytes >= chunkBytes || rows >= chunkRows) yield { batch: flush(), fraction: source.fraction() };
  }
  if (rows > 0 || columns.length === 0) yield { batch: flush(), fraction: 1 };
}

const nullStrings = (length: number): Data =>
  makeData({
    type: new Utf8(),
    length,
    nullCount: length,
    nullBitmap: new Uint8Array(Math.ceil(length / 8)),
    valueOffsets: new Int32Array(length + 1),
    data: new Uint8Array(0),
  });

async function* convertTyped(file: File, settings: Settings, columns: readonly OutputColumn[]): AsyncGenerator<Chunk> {
  let source: TypedSource;
  if (settings.format === "parquet") {
    // Rows per batch from the file's own width: about chunkBytes each.
    const probe = await parquetSource(file, { limit: 1024 });
    let bytesPerRow = 64;
    for await (const b of probe.batches) {
      bytesPerRow = Math.max(8, b.data.byteLength / Math.max(b.numRows, 1));
      break;
    }
    await probe.batches.return(undefined);
    const batchRows = Math.min(1_000_000, Math.max(1024, Math.floor(chunkBytes / bytesPerRow)));
    source = await parquetSource(file, { batchRows });
  } else {
    source = await arrowSource(file, settings);
  }
  const fields = columns.map((c) =>
    c.source === null
      ? new ArrowField(c.name, new Utf8(), true)
      : new ArrowField(c.name, source.schema.fields[c.source]!.type, true),
  );
  const schema = new Schema(fields);
  let any = false;
  for await (const b of source.batches) {
    const children = columns.map((c) => (c.source === null ? nullStrings(b.numRows) : b.data.children[c.source]!));
    const data = makeData({ type: new Struct(fields), length: b.numRows, nullCount: 0, children });
    any = true;
    yield { batch: new RecordBatch(schema, data), fraction: source.fraction() };
  }
  if (!any) {
    const data = makeData({
      type: new Struct(fields),
      length: 0,
      nullCount: 0,
      children: fields.map((f) => makeData({ type: f.type as DataType, length: 0, nullCount: 0 } as never)),
    });
    yield { batch: new RecordBatch(schema, data), fraction: 1 };
  }
}

export type { ColumnType };
