// The column types an upload offers, their Arrow types, and the cell values the readers hand
// over. Shared by the upload worker (which converts) and the dialog (which previews).

import {
  Bool,
  type DataType,
  DateDay,
  Decimal,
  Float64,
  Int64,
  TimeMicrosecond,
  TimeUnit,
  Timestamp,
  Utf8,
} from "apache-arrow";

/** The types a column can be loaded as; "SKIP" leaves it out. */
export const columnKinds = [
  "VARCHAR",
  "BIGINT",
  "DOUBLE",
  "DECIMAL",
  "BOOLEAN",
  "DATE",
  "TIMESTAMP",
  "TIMESTAMPTZ",
  "TIME",
] as const;
export type ColumnKind = (typeof columnKinds)[number];

export interface ColumnType {
  kind: ColumnKind;
  /** DECIMAL only: 1 to 38 digits, `scale` of them after the point. */
  precision?: number;
  scale?: number;
}

export const maxDecimalPrecision = 38;

/** How a type reads in SQL: DECIMAL(12,2), TIMESTAMP WITH TIME ZONE, … */
export function typeSql(type: ColumnType): string {
  switch (type.kind) {
    case "DECIMAL":
      return `DECIMAL(${type.precision ?? 18},${type.scale ?? 3})`;
    case "TIMESTAMPTZ":
      return "TIMESTAMP WITH TIME ZONE";
    default:
      return type.kind;
  }
}

/** The Arrow type that carries a column of `type` to the server (DuckDB maps it back exactly). */
export function arrowTypeFor(type: ColumnType): DataType {
  switch (type.kind) {
    case "VARCHAR":
      return new Utf8();
    case "BIGINT":
      return new Int64();
    case "DOUBLE":
      return new Float64();
    case "DECIMAL":
      return new Decimal(type.scale ?? 3, type.precision ?? 18, 128);
    case "BOOLEAN":
      return new Bool();
    case "DATE":
      return new DateDay();
    case "TIMESTAMP":
      return new Timestamp(TimeUnit.MICROSECOND);
    case "TIMESTAMPTZ":
      return new Timestamp(TimeUnit.MICROSECOND, "UTC");
    case "TIME":
      return new TimeMicrosecond();
  }
}

/**
 * The upload type closest to a server column type (an append converts the file's values to the
 * table's type here, so a value that doesn't fit is reported with its row and column), or
 * undefined for a type the upload doesn't offer (the server then converts VARCHAR).
 */
export function columnTypeOf(sqlType: string): ColumnType | undefined {
  const t = sqlType.trim().toUpperCase();
  const decimal = /^(?:DECIMAL|NUMERIC)\s*\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)$/.exec(t);
  if (decimal) return { kind: "DECIMAL", precision: Number(decimal[1]), scale: Number(decimal[2] ?? 0) };
  if (t === "DECIMAL" || t === "NUMERIC") return { kind: "DECIMAL", precision: 18, scale: 3 };
  if (/^(VARCHAR|TEXT|STRING|CHAR|BPCHAR)\b/.test(t)) return { kind: "VARCHAR" };
  if (/^(TINYINT|SMALLINT|INTEGER|INT|INT[1248]|BIGINT|LONG|SHORT|UTINYINT|USMALLINT|UINTEGER|UBIGINT)$/.test(t)) {
    return { kind: "BIGINT" };
  }
  if (/^(DOUBLE|FLOAT|FLOAT[48]|REAL|DOUBLE PRECISION)$/.test(t)) return { kind: "DOUBLE" };
  if (/^(BOOLEAN|BOOL|LOGICAL)$/.test(t)) return { kind: "BOOLEAN" };
  if (t === "DATE") return { kind: "DATE" };
  if (/^(TIMESTAMP WITH TIME ZONE|TIMESTAMPTZ)$/.test(t)) return { kind: "TIMESTAMPTZ" };
  if (/^(TIMESTAMP|DATETIME|TIMESTAMP_S|TIMESTAMP_MS|TIMESTAMP_NS|TIMESTAMP_US)$/.test(t)) return { kind: "TIMESTAMP" };
  if (t === "TIME") return { kind: "TIME" };
  return undefined;
}

export function sameType(a: ColumnType, b: ColumnType): boolean {
  return a.kind === b.kind && (a.kind !== "DECIMAL" || (a.precision === b.precision && a.scale === b.scale));
}

/**
 * A spreadsheet date, time or date-time: milliseconds since 1970-01-01 as the sheet shows it
 * (no time zone), and what the cell's number format shows of it.
 */
export interface SheetMoment {
  ms: number;
  shows: "date" | "time" | "datetime";
}

/** A nested JSON value (an object or array), kept as its JSON text. */
export interface JsonText {
  json: string;
}

/**
 * One cell as a reader hands it over: text (CSV, and strings everywhere), a number or a
 * boolean (Excel, JSON), a spreadsheet moment, nested JSON, or null (empty).
 */
export type Cell = null | string | number | boolean | SheetMoment | JsonText;

export function isMoment(v: Cell): v is SheetMoment {
  return typeof v === "object" && v !== null && "ms" in v;
}

export function isJsonText(v: Cell): v is JsonText {
  return typeof v === "object" && v !== null && "json" in v;
}

/** One column as a reader found it. */
export interface SourceColumn {
  name: string;
  /** The type it would load as. */
  inferred: ColumnType;
  /** A typed file's own type (Parquet, Arrow): the column loads with it, it can't be changed. */
  fixed?: string;
}

/** What the user chose for one of the file's columns. */
export interface ColumnChoice {
  name: string;
  type: ColumnType;
  skip: boolean;
}

/**
 * The columns the upload sends, in order: each from a file column (its index) or, for an append
 * to a table with a column the file lacks, all NULL (`source` null).
 */
export interface OutputColumn {
  name: string;
  type: ColumnType;
  source: number | null;
}
