// Converting one cell to a column type: text as it appears in CSV and JSON, and the numbers,
// booleans and dates a spreadsheet holds. A value that doesn't fit throws ValueError, which
// the converter reports with its row and column. Dates are computed here (no Date object,
// no time zone): a spreadsheet's dates are what it shows.

import { type Cell, type ColumnType, type SheetMoment, isJsonText, isMoment, typeSql } from "./types";

export class ValueError extends Error {}

/** A converted value: string, bigint (BIGINT, DECIMAL unscaled, TIMESTAMP and TIME µs), number, boolean. */
export type Value = string | bigint | number | boolean;

export interface ConvertContext {
  /** Numbers are spreadsheet serial dates when a date type is asked for (Excel). */
  serialDates?: boolean;
  /** The workbook counts dates from 1904-01-01 (old Mac Excel) rather than 1900. */
  epoch1904?: boolean;
}

const msPerDay = 86_400_000;
const int64Min = -(2n ** 63n);
const int64Max = 2n ** 63n - 1n;

/** Days since 1970-01-01 of a proleptic Gregorian date (H. Hinnant's days_from_civil). */
export function daysFromCivil(y: number, m: number, d: number): number {
  const yy = m <= 2 ? y - 1 : y;
  const era = Math.floor(yy / 400);
  const yoe = yy - era * 400;
  const doy = Math.floor((153 * (m + (m > 2 ? -3 : 9)) + 2) / 5) + d - 1;
  const doe = yoe * 365 + Math.floor(yoe / 4) - Math.floor(yoe / 100) + doy;
  return era * 146097 + doe - 719468;
}

/** The date of a day number (the inverse of daysFromCivil). */
export function civilFromDays(z: number): [number, number, number] {
  const zz = z + 719468;
  const era = Math.floor(zz / 146097);
  const doe = zz - era * 146097;
  const yoe = Math.floor((doe - Math.floor(doe / 1460) + Math.floor(doe / 36524) - Math.floor(doe / 146096)) / 365);
  const doy = doe - (365 * yoe + Math.floor(yoe / 4) - Math.floor(yoe / 100));
  const mp = Math.floor((5 * doy + 2) / 153);
  const d = doy - Math.floor((153 * mp + 2) / 5) + 1;
  const m = mp + (mp < 10 ? 3 : -9);
  return [era * 400 + yoe + (m <= 2 ? 1 : 0), m, d];
}

function validDate(y: number, m: number, d: number): boolean {
  if (y < 1 || y > 9999 || m < 1 || m > 12 || d < 1) return false;
  const [y2, m2, d2] = civilFromDays(daysFromCivil(y, m, d));
  return y2 === y && m2 === m && d2 === d;
}

const pad = (n: number, width = 2) => String(n).padStart(width, "0");

/** YYYY-MM-DD of a day number. */
export function dateText(days: number): string {
  const [y, m, d] = civilFromDays(days);
  return `${pad(y, 4)}-${pad(m)}-${pad(d)}`;
}

/** HH:MM:SS[.ffffff] of microseconds since midnight. */
export function timeText(micros: bigint): string {
  const us = Number(micros % 1_000_000n);
  const s = Number(micros / 1_000_000n);
  const base = `${pad(Math.floor(s / 3600))}:${pad(Math.floor(s / 60) % 60)}:${pad(s % 60)}`;
  return us === 0 ? base : `${base}.${String(us).padStart(6, "0").replace(/0+$/, "")}`;
}

/** YYYY-MM-DD HH:MM:SS[.ffffff] of microseconds since 1970 (UTC, or no zone). */
export function timestampText(micros: bigint): string {
  const day = 86_400_000_000n;
  let days = micros / day;
  let rest = micros % day;
  if (rest < 0n) {
    rest += day;
    days -= 1n;
  }
  return `${dateText(Number(days))} ${timeText(rest)}`;
}

/** A spreadsheet moment as text, as much of it as its format shows. */
export function momentText(m: SheetMoment): string {
  const micros = BigInt(Math.round(m.ms)) * 1000n;
  if (m.shows === "date") return dateText(Math.floor(m.ms / msPerDay));
  if (m.shows === "time") return timeText(((micros % 86_400_000_000n) + 86_400_000_000n) % 86_400_000_000n);
  return timestampText(micros);
}

/**
 * A spreadsheet serial date (days, with the time as the fraction) as milliseconds since 1970.
 * The 1900 system counts Lotus 1-2-3's 1900-02-29 (serial 60), which never was: it is refused.
 */
export function serialToMs(serial: number, epoch1904 = false): number {
  if (!Number.isFinite(serial)) throw new ValueError("not a date");
  if (epoch1904) return Math.round((serial - 24107) * msPerDay);
  if (serial >= 60 && serial < 61) throw new ValueError("1900-02-29, a day Excel counts that never was");
  return Math.round((serial - (serial < 60 ? 25568 : 25569)) * msPerDay);
}

// ── Text forms ────────────────────────────────────────────────────────────────────────────

const integerText = /^[+-]?\d+$/;
const numberRe = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;
const specialNumber = /^([+-]?)(inf|infinity|nan)$/i;
const dateTextRe = /^(\d{4})-(\d{1,2})-(\d{1,2})$/;
const timestampTextRe =
  /^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,9}))?)?)?\s*(Z|UTC|[+-]\d{2}(?::?\d{2})?)?$/i;
const timeTextRe = /^(\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?$/;
const trueText = new Set(["true", "t", "yes", "y", "1"]);
const falseText = new Set(["false", "f", "no", "n", "0"]);

export function parseBigint(text: string): bigint {
  const t = text.trim();
  if (!integerText.test(t)) throw new ValueError("not a whole number");
  const v = BigInt(t);
  if (v < int64Min || v > int64Max) throw new ValueError("outside BIGINT's range");
  return v;
}

export function parseDouble(text: string): number {
  const t = text.trim();
  if (numberRe.test(t)) return Number(t);
  const special = specialNumber.exec(t);
  if (special) {
    if (special[2]!.toLowerCase() === "nan") return Number.NaN;
    return special[1] === "-" ? -Infinity : Infinity;
  }
  throw new ValueError("not a number");
}

/** A decimal's unscaled value at `scale`, rounded half away from zero, at most `precision` digits. */
export function parseDecimal(text: string, precision: number, scale: number): bigint {
  const t = text.trim();
  const m = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(t);
  if (!m || (m[2] === "" && (m[3] ?? "") === "")) throw new ValueError("not a number");
  const negative = m[1] === "-";
  let digits = (m[2] ?? "") + (m[3] ?? "");
  // The point sits after `point` digits of `digits`.
  let point = (m[2] ?? "").length + Number(m[4] ?? 0);
  if (Math.abs(point) > 400) throw new ValueError(`too large for ${typeSql({ kind: "DECIMAL", precision, scale })}`);
  if (point < 0) {
    digits = "0".repeat(-point) + digits;
    point = 0;
  }
  if (point > digits.length) digits += "0".repeat(point - digits.length);
  const whole = digits.slice(0, point);
  let fraction = digits.slice(point);
  let roundUp = false;
  if (fraction.length > scale) {
    roundUp = fraction.charCodeAt(scale) >= 53; // "5"
    fraction = fraction.slice(0, scale);
  } else {
    fraction = fraction.padEnd(scale, "0");
  }
  let unscaled = BigInt((whole + fraction).replace(/^0+(?=\d)/, "") || "0");
  if (roundUp) unscaled += 1n;
  if (unscaled >= 10n ** BigInt(precision)) {
    throw new ValueError(`too large for ${typeSql({ kind: "DECIMAL", precision, scale })}`);
  }
  return negative ? -unscaled : unscaled;
}

export function parseBoolean(text: string): boolean {
  const t = text.trim().toLowerCase();
  if (trueText.has(t)) return true;
  if (falseText.has(t)) return false;
  throw new ValueError("not true or false");
}

/** Days since 1970 of YYYY-MM-DD. */
export function parseDate(text: string): number {
  const m = dateTextRe.exec(text.trim());
  if (!m) throw new ValueError("not a date (YYYY-MM-DD)");
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  if (!validDate(y, mo, d)) throw new ValueError("not a date that exists");
  return daysFromCivil(y, mo, d);
}

function fractionMicros(text: string | undefined): bigint {
  if (!text) return 0n;
  return BigInt(text.padEnd(6, "0").slice(0, 6));
}

/**
 * Microseconds since 1970 of `YYYY-MM-DD[ HH:MM[:SS[.f]]][offset]`, and whether it named an
 * offset (Z, UTC, +02, +0200, +02:00): with one, the value is converted to UTC.
 */
export function parseTimestamp(text: string): { micros: bigint; zoned: boolean } {
  const m = timestampTextRe.exec(text.trim());
  if (!m) throw new ValueError("not a date and time (YYYY-MM-DD HH:MM:SS)");
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  if (!validDate(y, mo, d)) throw new ValueError("not a date that exists");
  const h = Number(m[4] ?? 0);
  const mi = Number(m[5] ?? 0);
  const s = Number(m[6] ?? 0);
  if (h > 23 || mi > 59 || s > 59) throw new ValueError("not a time of day");
  let seconds = daysFromCivil(y, mo, d) * 86400 + h * 3600 + mi * 60 + s;
  const zone = m[8];
  if (zone && !/^(Z|UTC)$/i.test(zone)) {
    const z = /^([+-])(\d{2}):?(\d{2})?$/.exec(zone)!;
    const offset = (Number(z[2]) * 60 + Number(z[3] ?? 0)) * 60;
    if (offset > 18 * 3600) throw new ValueError("not a time zone offset");
    seconds -= z[1] === "-" ? -offset : offset;
  }
  return { micros: BigInt(seconds) * 1_000_000n + fractionMicros(m[7]), zoned: zone !== undefined };
}

/** Microseconds since midnight of HH:MM[:SS[.f]] (24:00:00 is the end of the day). */
export function parseTime(text: string): bigint {
  const m = timeTextRe.exec(text.trim());
  if (!m) throw new ValueError("not a time (HH:MM:SS)");
  const [h, mi, s] = [Number(m[1]), Number(m[2]), Number(m[3] ?? 0)];
  const frac = fractionMicros(m[4]);
  const endOfDay = h === 24 && mi === 0 && s === 0 && frac === 0n;
  if ((h > 23 && !endOfDay) || mi > 59 || s > 59) throw new ValueError("not a time of day");
  return BigInt(h * 3600 + mi * 60 + s) * 1_000_000n + frac;
}

// ── Conversion ────────────────────────────────────────────────────────────────────────────

/** A number's shortest exact text (no exponent for ordinary magnitudes). */
export function numberText(n: number): string {
  if (Number.isInteger(n) && Math.abs(n) < 1e21) return BigInt(n).toString();
  return String(n);
}

/** Text for a VARCHAR column: what the cell holds, dates as ISO text, nested JSON as JSON. */
export function cellText(cell: Exclude<Cell, null>): string {
  if (typeof cell === "string") return cell;
  if (typeof cell === "number") return numberText(cell);
  if (typeof cell === "boolean") return cell ? "true" : "false";
  if (isMoment(cell)) return momentText(cell);
  return cell.json;
}

/** A converter from cells to values of `type` (null stays null); throws ValueError. */
export function converter(type: ColumnType, ctx: ConvertContext = {}): (cell: Cell) => Value | null {
  const precision = type.precision ?? 18;
  const scale = type.scale ?? 3;
  const momentOf = (cell: Exclude<Cell, null>): SheetMoment | undefined => {
    if (isMoment(cell)) return cell;
    if (typeof cell === "number" && ctx.serialDates) return { ms: serialToMs(cell, ctx.epoch1904), shows: "datetime" };
    return undefined;
  };
  const refuse = (cell: Exclude<Cell, null>): never => {
    if (isMoment(cell)) throw new ValueError("a date or time, not a number");
    if (isJsonText(cell)) throw new ValueError("a nested JSON value");
    throw new ValueError(`not ${type.kind === "BOOLEAN" ? "true or false" : `a ${typeSql(type)}`}`);
  };
  switch (type.kind) {
    case "VARCHAR":
      return (cell) => (cell === null ? null : cellText(cell));
    case "BIGINT":
      return (cell) => {
        if (cell === null) return null;
        if (typeof cell === "string") return parseBigint(cell);
        if (typeof cell === "boolean") return cell ? 1n : 0n;
        if (typeof cell === "number") {
          if (!Number.isInteger(cell)) throw new ValueError("not a whole number");
          if (!Number.isSafeInteger(cell)) throw new ValueError("too large to be exact (more than 15 digits)");
          return BigInt(cell);
        }
        return refuse(cell);
      };
    case "DOUBLE":
      return (cell) => {
        if (cell === null) return null;
        if (typeof cell === "string") return parseDouble(cell);
        if (typeof cell === "number") return cell;
        if (typeof cell === "boolean") return cell ? 1 : 0;
        return refuse(cell);
      };
    case "DECIMAL":
      return (cell) => {
        if (cell === null) return null;
        if (typeof cell === "string") return parseDecimal(cell, precision, scale);
        if (typeof cell === "number") {
          if (!Number.isFinite(cell)) throw new ValueError("not a finite number");
          return parseDecimal(String(cell), precision, scale);
        }
        if (typeof cell === "boolean") return parseDecimal(cell ? "1" : "0", precision, scale);
        return refuse(cell);
      };
    case "BOOLEAN":
      return (cell) => {
        if (cell === null) return null;
        if (typeof cell === "boolean") return cell;
        if (typeof cell === "string") return parseBoolean(cell);
        if (typeof cell === "number" && (cell === 0 || cell === 1)) return cell === 1;
        return refuse(cell);
      };
    case "DATE":
      return (cell) => {
        if (cell === null) return null;
        if (typeof cell === "string") return parseDate(cell);
        const m = momentOf(cell);
        if (m) return Math.floor(m.ms / msPerDay);
        return refuse(cell);
      };
    case "TIMESTAMP":
    case "TIMESTAMPTZ":
      return (cell) => {
        if (cell === null) return null;
        if (typeof cell === "string") return parseTimestamp(cell).micros;
        const m = momentOf(cell);
        if (m) return BigInt(Math.round(m.ms)) * 1000n;
        return refuse(cell);
      };
    case "TIME":
      return (cell) => {
        if (cell === null) return null;
        if (typeof cell === "string") return parseTime(cell);
        const m = momentOf(cell);
        if (m) {
          const ms = ((Math.round(m.ms) % msPerDay) + msPerDay) % msPerDay;
          return BigInt(ms) * 1000n;
        }
        return refuse(cell);
      };
  }
}

/** A converted value as the preview shows it. */
export function valueText(type: ColumnType, value: Value): string {
  switch (type.kind) {
    case "DECIMAL": {
      const s = type.scale ?? 3;
      const v = value as bigint;
      const neg = v < 0n;
      const digits = (neg ? -v : v).toString().padStart(s + 1, "0");
      const text = s === 0 ? digits : `${digits.slice(0, -s)}.${digits.slice(-s)}`;
      return neg ? `-${text}` : text;
    }
    case "DATE":
      return dateText(value as number);
    case "TIMESTAMP":
      return timestampText(value as bigint);
    case "TIMESTAMPTZ":
      return `${timestampText(value as bigint)}+00`;
    case "TIME":
      return timeText(value as bigint);
    case "DOUBLE":
      return String(value);
    default:
      return String(value);
  }
}
