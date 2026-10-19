// Sorting the results grid: a comparator per Arrow type, on the values Vector.get returns (see
// format.ts). The grid puts NULLs last in both directions itself; these never see one.

import { type DataType, type Dictionary, Type } from "apache-arrow";

export type Compare = (a: unknown, b: unknown) => number;

const ordered = (a: number | bigint | string | boolean, b: number | bigint | string | boolean): number =>
  a < b ? -1 : a > b ? 1 : 0;

/** Numbers, bigints, or a mix of both (JavaScript compares them exactly); NaN after the rest. */
function numeric(a: unknown, b: unknown): number {
  const x = a as number | bigint;
  const y = b as number | bigint;
  const xNaN = typeof x === "number" && Number.isNaN(x);
  const yNaN = typeof y === "number" && Number.isNaN(y);
  if (xNaN || yNaN) return xNaN === yNaN ? 0 : xNaN ? 1 : -1;
  return ordered(x, y);
}

/** An Arrow big number (decimal, 128-bit integer) as a bigint (its unscaled digits). */
function big(v: unknown): bigint {
  if (typeof v === "bigint") return v;
  if (typeof v === "number") return BigInt(Math.trunc(v));
  return BigInt((v as { toString: () => string }).toString());
}

function bytes(a: unknown, b: unknown): number {
  const x = a as Uint8Array;
  const y = b as Uint8Array;
  const n = Math.min(x.length, y.length);
  for (let i = 0; i < n; i++) if (x[i] !== y[i]) return x[i]! - y[i]!;
  return x.length - y.length;
}

function interval(a: unknown, b: unknown): number {
  const x = a as ArrayLike<number>;
  const y = b as ArrayLike<number>;
  const n = Math.min(x.length, y.length);
  for (let i = 0; i < n; i++) if (x[i] !== y[i]) return x[i]! - y[i]!;
  return 0;
}

/**
 * How two values of `type` order. `text` gives a value's grid text, for the types that have no
 * natural order here (lists, structs, maps): those sort by what the grid shows.
 */
export function comparatorFor(type: DataType, text: (v: unknown) => string | null): Compare {
  switch (type.typeId) {
    case Type.Int:
    case Type.Float:
    case Type.Date:
    case Type.Timestamp:
    case Type.Time:
    case Type.Duration:
      return numeric;
    case Type.Decimal:
      return (a, b) => ordered(big(a), big(b));
    case Type.Bool:
      return (a, b) => ordered(Boolean(a), Boolean(b));
    case Type.Utf8:
    case Type.LargeUtf8:
      return (a, b) => ordered(String(a), String(b));
    case Type.Binary:
    case Type.LargeBinary:
    case Type.FixedSizeBinary:
      return bytes;
    case Type.Interval:
      return interval;
    case Type.Dictionary:
      return comparatorFor((type as Dictionary).dictionary as DataType, text);
    default:
      return (a, b) => ordered(text(a) ?? "", text(b) ?? "");
  }
}

const isNull = (v: unknown) => v === null || v === undefined;

/**
 * The row order for a sort on one column: `rows` (positions to sort) reordered, NULLs last in
 * both directions, ties kept in their original order.
 */
export function sortRows(
  rows: readonly number[],
  value: (row: number) => unknown,
  compare: Compare,
  direction: "asc" | "desc",
): number[] {
  const sign = direction === "asc" ? 1 : -1;
  return [...rows].sort((r1, r2) => {
    const a = value(r1);
    const b = value(r2);
    const an = isNull(a);
    const bn = isNull(b);
    if (an || bn) return an === bn ? 0 : an ? 1 : -1;
    return compare(a, b) * sign;
  });
}
