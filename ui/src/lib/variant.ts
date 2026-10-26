// Parquet VARIANT v1 (Arrow's arrow.parquet.variant). Decode only fields carrying
// that extension name; an ordinary struct named metadata/value is still a struct.
// https://github.com/apache/parquet-format/blob/master/VariantEncoding.md
import type { Field } from "apache-arrow";

export const isVariant = (field: Field): boolean =>
  field.metadata.get("ARROW:extension:name") === "arrow.parquet.variant";

export interface VariantValue {
  text: string;
  json: string;
}

const string = (text: string): VariantValue => ({ text, json: JSON.stringify(text) });
const literal = (text: string): VariantValue => ({ text, json: text });
const utf8 = new TextDecoder("utf-8", { fatal: true });

class Reader {
  constructor(readonly bytes: Uint8Array) {}
  check(at: number, size: number) {
    if (at < 0 || size < 0 || at + size > this.bytes.length) throw new Error("truncated value");
  }
  uint(at: number, size: number): number {
    this.check(at, size);
    let n = 0;
    for (let i = size - 1; i >= 0; i--) n = n * 256 + this.bytes[at + i]!;
    return n;
  }
  int(at: number, size: number): bigint {
    this.check(at, size);
    let n = 0n;
    for (let i = size - 1; i >= 0; i--) n = n * 256n + BigInt(this.bytes[at + i]!);
    return this.bytes[at + size - 1]! & 128 ? n - (1n << BigInt(size * 8)) : n;
  }
  text(at: number, size: number): string {
    this.check(at, size);
    return utf8.decode(this.bytes.subarray(at, at + size));
  }
}

function timestamp(n: bigint, nanos: boolean, utc: boolean): string {
  const perSecond = nanos ? 1_000_000_000n : 1_000_000n;
  let seconds = n / perSecond;
  let rest = n % perSecond;
  if (rest < 0n) {
    seconds--;
    rest += perSecond;
  }
  const date = new Date(Number(seconds * 1000n));
  if (!Number.isFinite(date.getTime())) throw new Error("timestamp out of range");
  const fraction = rest
    .toString()
    .padStart(nanos ? 9 : 6, "0")
    .replace(/0+$/, "");
  return date
    .toISOString()
    .replace("T", " ")
    .replace(/\.000Z$/, `${fraction ? `.${fraction}` : ""}${utc ? "+00" : ""}`);
}

function decode(metadata: Uint8Array, bytes: Uint8Array): VariantValue {
  const m = new Reader(metadata);
  const header = m.uint(0, 1);
  if ((header & 15) !== 1) throw new Error("unsupported metadata version");
  const width = (header >> 6) + 1;
  const count = m.uint(1, width);
  const base = 1 + width + (count + 1) * width;
  m.check(0, base);
  if (m.uint(1 + width, width) !== 0) throw new Error("invalid dictionary offset");
  const dictionary: string[] = [];
  for (let i = 0; i < count; i++) {
    const start = m.uint(1 + width + i * width, width);
    const end = m.uint(1 + width + (i + 1) * width, width);
    dictionary.push(m.text(base + start, end - start));
  }
  if (base + m.uint(1 + width + count * width, width) !== metadata.length) throw new Error("invalid dictionary length");
  const read = (data: Uint8Array, depth: number): VariantValue => {
    if (depth > 100) throw new Error("nesting exceeds 100 levels");
    const r = new Reader(data);
    const tag = r.uint(0, 1),
      basic = tag & 3,
      h = tag >> 2;
    if (basic === 1) return string(r.text(1, h));
    if (basic === 2 || basic === 3) {
      const object = basic === 2;
      const sizeWidth = h & (object ? 16 : 4) ? 4 : 1;
      const offsetWidth = (h & 3) + 1;
      const idWidth = object ? ((h >> 2) & 3) + 1 : 0;
      const n = r.uint(1, sizeWidth);
      const ids = 1 + sizeWidth,
        offsets = ids + n * idWidth;
      const values = offsets + (n + 1) * offsetWidth;
      r.check(0, values);
      const total = r.uint(offsets + n * offsetWidth, offsetWidth);
      r.check(values, total);
      // Object values may be physically out of order. Find each end by its next
      // physical offset, rather than the next field in key order.
      const starts = Array.from({ length: n }, (_, i) => r.uint(offsets + i * offsetWidth, offsetWidth));
      const physical = [...starts, total].sort((a, b) => a - b);
      if (physical[0] !== 0 || new Set(physical).size !== physical.length) throw new Error("invalid field offsets");
      const ends = new Map(physical.slice(0, -1).map((s, i) => [s, physical[i + 1]!]));
      const parts: string[] = [];
      const keys = new Set<string>();
      for (let i = 0; i < n; i++) {
        const start = starts[i]!,
          end = ends.get(start)!;
        if (end > total || end <= start) throw new Error("invalid field offset");
        const value = read(data.subarray(values + start, values + end), depth + 1).json;
        if (object) {
          const key = dictionary[r.uint(ids + i * idWidth, idWidth)];
          if (key === undefined || keys.has(key)) throw new Error("invalid object key");
          keys.add(key);
          parts.push(`${JSON.stringify(key)}:${value}`);
        } else parts.push(value);
      }
      return literal(`${object ? "{" : "["}${parts.join(",")}${object ? "}" : "]"}`);
    }
    if (h === 0) return literal("null");
    if (h === 1 || h === 2) return literal(h === 1 ? "true" : "false");
    if (h >= 3 && h <= 6) return literal(r.int(1, 2 ** (h - 3)).toString());
    if (h === 7 || h === 14) {
      const size = h === 7 ? 8 : 4;
      r.check(1, size);
      const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
      const n = h === 7 ? view.getFloat64(1, true) : view.getFloat32(1, true);
      return Number.isFinite(n) ? literal(String(n)) : string(String(n));
    }
    if (h >= 8 && h <= 10) {
      const scale = r.uint(1, 1);
      if (scale > 38) throw new Error("invalid decimal scale");
      const n = r.int(2, 2 ** (h - 6));
      const digits = (n < 0n ? -n : n).toString().padStart(scale + 1, "0");
      return literal(`${n < 0n ? "-" : ""}${scale ? `${digits.slice(0, -scale)}.${digits.slice(-scale)}` : digits}`);
    }
    if (h === 11) {
      const date = new Date(Number(r.int(1, 4)) * 86400000);
      if (!Number.isFinite(date.getTime())) throw new Error("date out of range");
      return string(date.toISOString().split("T")[0]!);
    }
    if ([12, 13, 18, 19].includes(h)) return string(timestamp(r.int(1, 8), h >= 18, h === 12 || h === 18));
    if (h === 15 || h === 16) {
      const size = r.uint(1, 4);
      r.check(5, size);
      return string(
        h === 16
          ? r.text(5, size)
          : `\\x${Array.from(data.subarray(5, 5 + size), (b) => b.toString(16).padStart(2, "0")).join("")}`,
      );
    }
    if (h === 17) {
      const n = r.int(1, 8);
      if (n < 0n || n >= 86400000000n) throw new Error("time out of range");
      return string(timestamp(n, false, false).split(" ")[1]!);
    }
    if (h === 20) {
      r.check(1, 16);
      const hex = Array.from(data.subarray(1, 17), (b) => b.toString(16).padStart(2, "0")).join("");
      return string(
        `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`,
      );
    }
    throw new Error(`unsupported primitive ${h}`);
  };
  return read(bytes, 0);
}

/** Never fail an entire result because one encoded cell is malformed or from a newer spec. */
export function variantValue(value: unknown): VariantValue {
  try {
    const row = value as { metadata?: unknown; value?: unknown; typed_value?: unknown };
    if (!(row?.metadata instanceof Uint8Array) || !(row.value instanceof Uint8Array) || row.typed_value != null)
      throw new Error("unsupported VARIANT storage (expected unshredded binary metadata/value)");
    return decode(row.metadata, row.value);
  } catch (error) {
    return string(`[VARIANT: ${error instanceof Error ? error.message : "invalid value"}]`);
  }
}
