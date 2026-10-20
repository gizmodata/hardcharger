// JSON uploads: JSON lines (one object per line, read as it streams) or one array of objects
// (read whole). Each object is a row and its keys are the columns, in the order they first
// appear; a nested object or array is kept as its JSON text. Integers too large for a
// JavaScript number keep every digit (as text) where the browser lets JSON.parse see the
// source text.

import type { Cell } from "./types";

export class JsonError extends Error {}

type Reviver = (this: unknown, key: string, value: unknown, context?: { source?: string }) => unknown;

/** Keeps a big integer's digits: JSON.parse would round it to the nearest double. */
const keepBigIntegers: Reviver = (_key, value, context) => {
  if (typeof value === "number" && Number.isInteger(value) && !Number.isSafeInteger(value) && context?.source) {
    return /^-?\d+$/.test(context.source) ? context.source : value;
  }
  return value;
};

export function parseJson(text: string): unknown {
  return JSON.parse(text, keepBigIntegers);
}

/** A JSON value as a cell. */
export function jsonCell(value: unknown): Cell {
  if (value === null || value === undefined) return null;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return value;
  return { json: JSON.stringify(value) };
}

/** The keys of objects, in first-seen order, grown as rows arrive. */
export class JsonColumns {
  readonly names: string[] = [];
  readonly #index = new Map<string, number>();
  /** Whether new keys may still be added (false once the columns are settled). */
  open = true;

  /** The row as cells in column order; throws on a key the settled columns lack. */
  row(value: unknown, where: string): Cell[] {
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      throw new JsonError(`${where} is not a JSON object: each row of a JSON upload is an object.`);
    }
    const out: Cell[] = new Array<Cell>(this.names.length).fill(null);
    for (const [key, v] of Object.entries(value as Record<string, unknown>)) {
      let i = this.#index.get(key);
      if (i === undefined) {
        if (!this.open) {
          throw new JsonError(
            `${where} has the key "${key}", which no row before it had: the columns come from the first ` +
              "rows of the file. Put the key in an early row (or remove it) and upload again.",
          );
        }
        i = this.names.length;
        this.names.push(key);
        this.#index.set(key, i);
      }
      out[i] = jsonCell(v);
    }
    return out;
  }
}

/** Splits text arriving in chunks into lines (\n or \r\n). */
export class LineSplitter {
  #rest = "";
  #line = 0;
  readonly #onLine: (line: string, number: number) => void;
  constructor(onLine: (line: string, number: number) => void) {
    this.#onLine = onLine;
  }

  push(text: string): void {
    let start = 0;
    const all = this.#rest + text;
    for (let i = all.indexOf("\n"); i !== -1; i = all.indexOf("\n", start)) {
      this.#line++;
      this.#onLine(all.slice(start, i).replace(/\r$/, ""), this.#line);
      start = i + 1;
    }
    this.#rest = all.slice(start);
    if (this.#rest.length > 64 << 20) throw new JsonError(`Line ${this.#line + 1} is longer than 64 MiB.`);
  }

  end(): void {
    if (this.#rest !== "") {
      this.#line++;
      this.#onLine(this.#rest.replace(/\r$/, ""), this.#line);
      this.#rest = "";
    }
  }
}
