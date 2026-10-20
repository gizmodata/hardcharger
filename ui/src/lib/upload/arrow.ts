// Record batches from converted values: each column's buffers written directly (no per-value
// Arrow builder), so a batch of a few megabytes is cheap to make in the upload worker.

import { type Data, type DataType, Field, RecordBatch, Schema, Struct, Utf8, makeData } from "apache-arrow";

import { arrowTypeFor, type ColumnType, type OutputColumn } from "./types";
import type { Value } from "./values";

const encoder = new TextEncoder();
const mask64 = (1n << 64n) - 1n;

class Bits {
  bytes = new Uint8Array(64);
  set(i: number): void {
    const byte = i >> 3;
    if (byte >= this.bytes.length) {
      const grown = new Uint8Array(Math.max(this.bytes.length * 2, byte + 1));
      grown.set(this.bytes);
      this.bytes = grown;
    }
    this.bytes[byte]! |= 1 << (i & 7);
  }
  take(length: number): Uint8Array {
    const out = new Uint8Array(Math.ceil(length / 8));
    out.set(this.bytes.subarray(0, out.length));
    return out;
  }
}

type Typed = Int32Array | Float64Array | BigInt64Array | BigUint64Array;

function grow<T extends Typed>(a: T, need: number, make: (n: number) => T): T {
  if (need <= a.length) return a;
  const b = make(Math.max(need, a.length * 2));
  b.set(a as never);
  return b;
}

/**
 * The first `n` values of `a`, as an array of exactly `n` (zeros past what was written: the
 * slots of trailing NULLs, which Arrow still needs room for).
 */
function exactly<T extends Typed>(a: T, n: number, make: (n: number) => T): T {
  const out = make(n);
  out.set(a.subarray(0, Math.min(n, a.length)) as never);
  return out;
}

/** One column's values, appended a row at a time. */
export class ColumnBuffer {
  readonly type: ColumnType;
  readonly arrowType: DataType;
  length = 0;
  nullCount = 0;
  /** About how many bytes the column holds so far. */
  bytes = 0;
  #valid = new Bits();
  #strings: string[] = [];
  #i32 = new Int32Array(0);
  #f64 = new Float64Array(0);
  #i64 = new BigInt64Array(0);
  #dec = new BigUint64Array(0);
  #bools = new Bits();

  constructor(type: ColumnType) {
    this.type = type;
    this.arrowType = arrowTypeFor(type);
  }

  push(value: Value | null): void {
    const i = this.length++;
    if (value === null) {
      this.nullCount++;
      if (this.type.kind === "VARCHAR") this.#strings.push("");
      this.bytes += 1;
      return;
    }
    this.#valid.set(i);
    switch (this.type.kind) {
      case "VARCHAR": {
        const s = value as string;
        this.#strings.push(s);
        this.bytes += s.length + 4;
        return;
      }
      case "DOUBLE":
        this.#f64 = grow(this.#f64, i + 1, (n) => new Float64Array(n));
        this.#f64[i] = value as number;
        this.bytes += 8;
        return;
      case "DATE":
        this.#i32 = grow(this.#i32, i + 1, (n) => new Int32Array(n));
        this.#i32[i] = value as number;
        this.bytes += 4;
        return;
      case "BOOLEAN":
        if (value) this.#bools.set(i);
        this.bytes += 1;
        return;
      case "DECIMAL": {
        this.#dec = grow(this.#dec, 2 * (i + 1), (n) => new BigUint64Array(n));
        const v = value as bigint;
        this.#dec[2 * i] = v & mask64;
        this.#dec[2 * i + 1] = (v >> 64n) & mask64;
        this.bytes += 16;
        return;
      }
      default:
        this.#i64 = grow(this.#i64, i + 1, (n) => new BigInt64Array(n));
        this.#i64[i] = value as bigint;
        this.bytes += 8;
    }
  }

  /** The column as Arrow data; the buffer starts over. */
  finish(): Data {
    const length = this.length;
    const nullCount = this.nullCount;
    const nullBitmap = nullCount > 0 ? this.#valid.take(length) : undefined;
    const type = this.arrowType;
    let data: Data;
    switch (this.type.kind) {
      case "VARCHAR": {
        const offsets = new Int32Array(length + 1);
        const parts = this.#strings.map((s) => encoder.encode(s));
        let total = 0;
        for (let i = 0; i < length; i++) {
          total += parts[i]!.length;
          offsets[i + 1] = total;
        }
        const bytes = new Uint8Array(total);
        let at = 0;
        for (const p of parts) {
          bytes.set(p, at);
          at += p.length;
        }
        data = makeData({ type: type as Utf8, length, nullCount, nullBitmap, valueOffsets: offsets, data: bytes });
        break;
      }
      case "DOUBLE":
        data = makeData({
          type,
          length,
          nullCount,
          nullBitmap,
          data: exactly(this.#f64, length, (n) => new Float64Array(n)),
        } as never);
        break;
      case "DATE":
        data = makeData({
          type,
          length,
          nullCount,
          nullBitmap,
          data: exactly(this.#i32, length, (n) => new Int32Array(n)),
        } as never);
        break;
      case "BOOLEAN":
        data = makeData({ type, length, nullCount, nullBitmap, data: this.#bools.take(length) } as never);
        break;
      case "DECIMAL":
        data = makeData({
          type,
          length,
          nullCount,
          nullBitmap,
          data: new Uint32Array(exactly(this.#dec, 2 * length, (n) => new BigUint64Array(n)).buffer),
        } as never);
        break;
      default:
        data = makeData({
          type,
          length,
          nullCount,
          nullBitmap,
          data: exactly(this.#i64, length, (n) => new BigInt64Array(n)),
        } as never);
    }
    this.length = 0;
    this.nullCount = 0;
    this.bytes = 0;
    this.#valid = new Bits();
    this.#bools = new Bits();
    this.#strings = [];
    return data;
  }
}

/** The schema an upload sends. */
export function outputSchema(columns: readonly OutputColumn[]): Schema {
  return new Schema(columns.map((c) => new Field(c.name, arrowTypeFor(c.type), true)));
}

/** A record batch of finished columns. */
export function recordBatch(schema: Schema, children: Data[], length: number): RecordBatch {
  const data = makeData({ type: new Struct(schema.fields), length, nullCount: 0, children });
  return new RecordBatch(schema, data);
}
