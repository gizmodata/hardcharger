// Turns Arrow values (as apache-arrow's Vector.get returns them) into the text the grid,
// the clipboard and CSV/TSV show, in DuckDB's own style, and into exact JSON.
//
// What Vector.get returns, by type (apache-arrow 21): integers up to 32 bits and floats as
// numbers, 64-bit integers as bigints, decimals as an unscaled DecimalBigNum, dates and
// timestamps as (fractional) milliseconds since the epoch, 64-bit times as bigints, the
// month-day-nano interval as an Int32Array, binary as Uint8Array, lists as Vectors,
// structs as StructRows, maps as MapRows, and dictionary values decoded.

import {
  DataType,
  type Decimal,
  type Dictionary,
  type Field,
  type FixedSizeList,
  type Float,
  type Int,
  Precision,
  type Schema,
  type Time,
  TimeUnit,
  type Timestamp,
  Type,
} from "apache-arrow";

import { isVariant, variantValue } from "./variant";

export type Format = (value: unknown) => string;

const pad = (n: number, width = 2) => String(Math.trunc(Math.abs(n))).padStart(width, "0");

function fraction(digits: string): string {
  const trimmed = digits.replace(/0+$/, "");
  return trimmed === "" ? "" : `.${trimmed}`;
}

function year(y: number): string {
  // DuckDB prints years before 1 as "(BC)".
  return y > 0 ? pad(y, 4) : pad(1 - y, 4);
}
function era(y: number): string {
  return y > 0 ? "" : " (BC)";
}

/** A date from milliseconds since the epoch (UTC). */
export function formatDate(ms: number): string {
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return String(ms);
  const y = d.getUTCFullYear();
  return `${year(y)}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}${era(y)}`;
}

/** Splits an epoch time in `unit` into whole UTC milliseconds and the sub-millisecond digits. */
function splitEpoch(value: number | bigint, unit: TimeUnit): { ms: number; sub: string } {
  // Vector.get gives timestamps as (fractional) milliseconds; take the digits it carries
  // exactly: microseconds fit a double exactly until the year 2255.
  if (typeof value === "bigint") {
    const perMs = unit === TimeUnit.NANOSECOND ? 1_000_000n : unit === TimeUnit.MICROSECOND ? 1000n : 1n;
    const scaled = unit === TimeUnit.SECOND ? value * 1000n : value;
    let ms = scaled / perMs;
    let rest = scaled % perMs;
    if (rest < 0n) {
      ms -= 1n;
      rest += perMs;
    }
    const width = unit === TimeUnit.NANOSECOND ? 6 : unit === TimeUnit.MICROSECOND ? 3 : 0;
    return { ms: Number(ms), sub: width ? String(rest).padStart(width, "0") : "" };
  }
  if (unit === TimeUnit.MICROSECOND || unit === TimeUnit.NANOSECOND) {
    const us = Math.round(value * 1000);
    const ms = Math.floor(us / 1000);
    return { ms, sub: pad(us - ms * 1000, 3) };
  }
  return { ms: Math.floor(value), sub: "" };
}

function wallClock(ms: number, sub: string, timeZone: string | null | undefined): string {
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return String(ms);
  let offset = "";
  let parts = {
    y: d.getUTCFullYear(),
    mo: d.getUTCMonth() + 1,
    da: d.getUTCDate(),
    h: d.getUTCHours(),
    mi: d.getUTCMinutes(),
    s: d.getUTCSeconds(),
  };
  if (timeZone) {
    const local = zoned(d, timeZone);
    if (local) {
      parts = local.parts;
      offset = local.offset;
    } else {
      offset = "+00";
    }
  }
  const frac = fraction(pad(d.getUTCMilliseconds(), 3) + sub);
  return `${year(parts.y)}-${pad(parts.mo)}-${pad(parts.da)} ${pad(parts.h)}:${pad(parts.mi)}:${pad(parts.s)}${frac}${offset}${era(parts.y)}`;
}

const zoneFormats = new Map<string, Intl.DateTimeFormat | null>();

function zoned(d: Date, timeZone: string) {
  let f = zoneFormats.get(timeZone);
  if (f === undefined) {
    try {
      f = new Intl.DateTimeFormat("en-US", {
        timeZone,
        hourCycle: "h23",
        year: "numeric",
        month: "numeric",
        day: "numeric",
        hour: "numeric",
        minute: "numeric",
        second: "numeric",
        era: "short",
      });
    } catch {
      f = null; // a zone this browser doesn't know: show UTC
    }
    zoneFormats.set(timeZone, f);
  }
  if (f === null) return undefined;
  const get = (type: string) => f.formatToParts(d).find((p) => p.type === type)?.value ?? "0";
  const bc = get("era").startsWith("B");
  const y = Number(get("year"));
  const parts = {
    y: bc ? 1 - y : y,
    mo: Number(get("month")),
    da: Number(get("day")),
    h: Number(get("hour")),
    mi: Number(get("minute")),
    s: Number(get("second")),
  };
  const localMs = Date.UTC(parts.y, parts.mo - 1, parts.da, parts.h, parts.mi, parts.s);
  const minutes = Math.round((localMs - Math.floor(d.getTime() / 1000) * 1000) / 60000);
  const sign = minutes < 0 ? "-" : "+";
  const abs = Math.abs(minutes);
  const offset = `${sign}${pad(Math.floor(abs / 60))}${abs % 60 ? `:${pad(abs % 60)}` : ""}`;
  return { parts, offset };
}

/** A time of day from a count of `unit`s since midnight. */
export function formatTime(value: number | bigint, unit: TimeUnit): string {
  const perSecond =
    unit === TimeUnit.NANOSECOND
      ? 1_000_000_000n
      : unit === TimeUnit.MICROSECOND
        ? 1_000_000n
        : unit === TimeUnit.MILLISECOND
          ? 1000n
          : 1n;
  const v = typeof value === "bigint" ? value : BigInt(Math.round(value));
  const seconds = Number(v / perSecond);
  const rest = v % perSecond;
  const width = String(perSecond).length - 1;
  const frac = width ? fraction(String(rest).padStart(width, "0")) : "";
  return `${pad(Math.floor(seconds / 3600))}:${pad(Math.floor(seconds / 60) % 60)}:${pad(seconds % 60)}${frac}`;
}

/** DuckDB's interval text: "1 year 2 months 3 days 04:05:06.5". */
export function formatInterval(months: number, days: number, nanos: bigint): string {
  const out: string[] = [];
  const plural = (n: number, word: string) => `${n} ${word}${Math.abs(n) === 1 ? "" : "s"}`;
  const years = Math.trunc(months / 12);
  const mons = months % 12;
  if (years) out.push(plural(years, "year"));
  if (mons) out.push(plural(mons, "month"));
  if (days) out.push(plural(days, "day"));
  if (nanos !== 0n || out.length === 0) {
    const negative = nanos < 0n;
    const abs = negative ? -nanos : nanos;
    const seconds = abs / 1_000_000_000n;
    const sub = abs % 1_000_000_000n;
    const h = seconds / 3600n;
    const text = `${String(h).padStart(2, "0")}:${pad(Number((seconds / 60n) % 60n))}:${pad(Number(seconds % 60n))}${fraction(String(sub).padStart(9, "0").slice(0, 6))}`;
    out.push(negative ? `-${text}` : text);
  }
  return out.join(" ");
}

/** An unscaled decimal integer (as text) with `scale` digits after the point. */
export function scaleDecimal(unscaled: string, scale: number): string {
  if (scale <= 0) return scale === 0 ? unscaled : unscaled + "0".repeat(-scale);
  const negative = unscaled.startsWith("-");
  const digits = (negative ? unscaled.slice(1) : unscaled).padStart(scale + 1, "0");
  const text = `${digits.slice(0, digits.length - scale)}.${digits.slice(digits.length - scale)}`;
  return negative ? `-${text}` : text;
}

/** BLOB text as DuckDB prints it: printable ASCII as is, other bytes as \xHH. */
export function formatBinary(bytes: Uint8Array): string {
  let out = "";
  for (const b of bytes) {
    out +=
      b >= 32 && b < 127 && b !== 92 ? String.fromCharCode(b) : `\\x${b.toString(16).toUpperCase().padStart(2, "0")}`;
  }
  return out;
}

function intervalParts(type: DataType, value: unknown): [number, number, bigint] | undefined {
  if (!(value instanceof Int32Array)) return undefined;
  const t = type as { unit?: number };
  // IntervalUnit: YEAR_MONTH 0, DAY_TIME 1, MONTH_DAY_NANO 2.
  if (t.unit === 0) return [value[0] ?? 0, 0, 0n];
  if (t.unit === 1) return [0, value[0] ?? 0, BigInt(value[1] ?? 0) * 1_000_000n];
  const lo = BigInt((value[2] ?? 0) >>> 0);
  const hi = BigInt(value[3] ?? 0);
  return [value[0] ?? 0, value[1] ?? 0, (hi << 32n) | lo];
}

const isNull = (v: unknown) => v === null || v === undefined;

/** A field's type (apache-arrow types Field.type loosely). */
const typeOf = (f: Field): DataType => f.type as DataType;

/** An Arrow big number (decimal, 128-bit integer) as its integer digits. */
function bigNumText(v: unknown): string {
  return (v as { toString: () => string }).toString();
}

/** Any other value as text: primitives as themselves, objects as JSON. */
function text(v: unknown): string {
  switch (typeof v) {
    case "string":
      return v;
    case "number":
    case "bigint":
    case "boolean":
      return v.toString();
    default:
      return JSON.stringify(v, jsonSafe) ?? "";
  }
}

/** How a value of `type` reads as text: NULL is `null` (the caller decides how to show it). */
export function textFor(source: DataType | Field): (value: unknown) => string | null {
  if (!(source instanceof DataType) && isVariant(source)) return (v) => (isNull(v) ? null : variantValue(v).text);
  const type = source instanceof DataType ? source : typeOf(source);
  switch (type.typeId) {
    case Type.Null:
      return () => null;
    case Type.Dictionary:
      return textFor((type as Dictionary).dictionary as DataType);
    case Type.Decimal: {
      const scale = (type as Decimal).scale;
      return (v) => (isNull(v) ? null : scaleDecimal(bigNumText(v), scale));
    }
    case Type.Date:
      return (v) => (isNull(v) ? null : formatDate(Number(v)));
    case Type.Timestamp: {
      const t = type as Timestamp;
      return (v) => {
        if (isNull(v)) return null;
        const { ms, sub } = splitEpoch(v as number | bigint, t.unit);
        return wallClock(ms, sub, t.timezone);
      };
    }
    case Type.Time: {
      const unit = (type as Time).unit;
      return (v) => (isNull(v) ? null : formatTime(v as number | bigint, unit));
    }
    case Type.Interval:
      return (v) => {
        if (isNull(v)) return null;
        const parts = intervalParts(type, v);
        return parts ? formatInterval(...parts) : text(v);
      };
    case Type.Binary:
    case Type.LargeBinary:
    case Type.FixedSizeBinary:
      return (v) => (isNull(v) ? null : v instanceof Uint8Array ? formatBinary(v) : text(v));
    case Type.List:
    case Type.FixedSizeList:
    case Type.LargeList: {
      const child = (type as DataType<Type.List>).children[0]!;
      const inner = nestedText(child);
      return (v) => {
        if (isNull(v)) return null;
        const items: string[] = [];
        for (const item of v as Iterable<unknown>) items.push(inner(item));
        return `[${items.join(", ")}]`;
      };
    }
    case Type.Struct: {
      const fields = (type as DataType<Type.Struct>).children;
      const inner = fields.map((f) => nestedText(f));
      return (v) => {
        if (isNull(v)) return null;
        const row = v as Record<string, unknown>;
        return `{${fields.map((f, i) => `'${f.name}': ${inner[i]!(row[f.name])}`).join(", ")}}`;
      };
    }
    case Type.Map: {
      const entry = typeOf((type as DataType<Type.Map>).children[0]!) as DataType<Type.Struct>;
      const key = nestedText(entry.children[0]!);
      const value = nestedText(entry.children[1]!);
      return (v) => {
        if (isNull(v)) return null;
        const pairs: string[] = [];
        for (const [k, x] of v as Iterable<[unknown, unknown]>) pairs.push(`${key(k)}=${value(x)}`);
        return `{${pairs.join(", ")}}`;
      };
    }
    case Type.Float:
      return (v) => (isNull(v) ? null : floatText(v as number));
    default:
      return (v) => (isNull(v) ? null : text(v));
  }
}

function floatText(v: number): string {
  if (Number.isNaN(v)) return "nan";
  if (v === Infinity) return "inf";
  if (v === -Infinity) return "-inf";
  return String(v);
}

function nestedText(type: DataType | Field): (v: unknown) => string {
  const text = textFor(type);
  return (v) => text(v) ?? "NULL";
}

function jsonSafe(_key: string, value: unknown): unknown {
  return typeof value === "bigint" ? value.toString() : value;
}

/** A value as JSON text: exact numbers (decimals and bigints keep every digit), and the
 * grid's text for dates, times, intervals and binary. */
export function jsonFor(source: DataType | Field): (value: unknown) => string {
  if (!(source instanceof DataType) && isVariant(source)) return (v) => (isNull(v) ? "null" : variantValue(v).json);
  const type = source instanceof DataType ? source : typeOf(source);
  switch (type.typeId) {
    case Type.Dictionary:
      return jsonFor((type as Dictionary).dictionary as DataType);
    case Type.Int:
    case Type.Decimal: {
      const text = textFor(type);
      return (v) => (isNull(v) ? "null" : text(v)!);
    }
    case Type.Float:
      return (v) => (isNull(v) ? "null" : Number.isFinite(v) ? text(v) : JSON.stringify(floatText(v as number)));
    case Type.Bool:
      return (v) => (isNull(v) ? "null" : v ? "true" : "false");
    case Type.List:
    case Type.FixedSizeList:
    case Type.LargeList: {
      const inner = jsonFor((type as DataType<Type.List>).children[0]!);
      return (v) => {
        if (isNull(v)) return "null";
        const items: string[] = [];
        for (const item of v as Iterable<unknown>) items.push(inner(item));
        return `[${items.join(",")}]`;
      };
    }
    case Type.Struct: {
      const fields = (type as DataType<Type.Struct>).children;
      const inner = fields.map((f) => jsonFor(f));
      return (v) => {
        if (isNull(v)) return "null";
        const row = v as Record<string, unknown>;
        return `{${fields.map((f, i) => `${JSON.stringify(f.name)}:${inner[i]!(row[f.name])}`).join(",")}}`;
      };
    }
    case Type.Map: {
      const entry = typeOf((type as DataType<Type.Map>).children[0]!) as DataType<Type.Struct>;
      const key = textFor(typeOf(entry.children[0]!));
      const value = jsonFor(entry.children[1]!);
      return (v) => {
        if (isNull(v)) return "null";
        const pairs: string[] = [];
        for (const [k, x] of v as Iterable<[unknown, unknown]>)
          pairs.push(`${JSON.stringify(key(k) ?? "NULL")}:${value(x)}`);
        return `{${pairs.join(",")}}`;
      };
    }
    default: {
      const text = textFor(type);
      return (v) => {
        const t = text(v);
        return t === null ? "null" : JSON.stringify(t);
      };
    }
  }
}

/** The SQL type name to show in a column header: the server's, when its metadata says. */
export function typeLabel(field: Field): string {
  if (isVariant(field)) return "VARIANT";
  const named = field.metadata.get("ARROW:FLIGHT:SQL:TYPE_NAME");
  if (named) return named;
  return arrowTypeLabel(typeOf(field));
}

export function arrowTypeLabel(type: DataType): string {
  switch (type.typeId) {
    case Type.Int: {
      const t = type as Int;
      const names: Record<number, string> = { 8: "TINYINT", 16: "SMALLINT", 32: "INTEGER", 64: "BIGINT" };
      return `${t.isSigned ? "" : "U"}${names[t.bitWidth] ?? "INTEGER"}`;
    }
    case Type.Float:
      return (type as Float).precision === Precision.DOUBLE ? "DOUBLE" : "FLOAT";
    case Type.Decimal: {
      const t = type as Decimal;
      return `DECIMAL(${t.precision},${t.scale})`;
    }
    case Type.Utf8:
    case Type.LargeUtf8:
      return "VARCHAR";
    case Type.Bool:
      return "BOOLEAN";
    case Type.Date:
      return "DATE";
    case Type.Time:
      return "TIME";
    case Type.Timestamp:
      return (type as Timestamp).timezone ? "TIMESTAMP WITH TIME ZONE" : "TIMESTAMP";
    case Type.Interval:
      return "INTERVAL";
    case Type.Binary:
    case Type.LargeBinary:
    case Type.FixedSizeBinary:
      return "BLOB";
    case Type.Dictionary:
      return arrowTypeLabel((type as Dictionary).dictionary as DataType);
    case Type.List:
    case Type.LargeList:
      return `${typeLabel((type as DataType<Type.List>).children[0]!)}[]`;
    case Type.FixedSizeList: {
      const t = type as FixedSizeList;
      return `${typeLabel(t.children[0]!)}[${t.listSize}]`;
    }
    case Type.Struct:
      return `STRUCT(${(type as DataType<Type.Struct>).children.map((f) => `${f.name} ${typeLabel(f)}`).join(", ")})`;
    case Type.Map:
      return "MAP";
    case Type.Null:
      return "NULL";
    default:
      return Type[type.typeId];
  }
}

/** Whether a column's values are numbers (the grid aligns them to the right). */
export function isNumeric(type: DataType): boolean {
  return DataType.isInt(type) || DataType.isFloat(type) || DataType.isDecimal(type);
}

/** Text formatters for every column of a schema. */
export function columnText(schema: Schema): ((value: unknown) => string | null)[] {
  return schema.fields.map((f) => textFor(f));
}

/** A session idle timeout in words: "15 minutes", "90 seconds", "2 hours" (0 is the caller's case). */
export function idleTimeoutText(seconds: number): string {
  const unit = (n: number, word: string) => `${n.toLocaleString("en-US")} ${word}${n === 1 ? "" : "s"}`;
  if (seconds % 3600 === 0) return unit(seconds / 3600, "hour");
  if (seconds % 60 === 0) return unit(seconds / 60, "minute");
  return unit(seconds, "second");
}
