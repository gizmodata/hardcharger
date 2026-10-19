// The grid's text of each row of a result, made when first asked for and kept (the filter
// reads every row once; drawing only the rows in view).

import type { Table, Vector } from "apache-arrow";

import { columnText } from "./format";

export class RowTexts {
  readonly #vectors: Vector[];
  readonly #formats: ((value: unknown) => string | null)[];
  readonly #rows = new Map<number, (string | null)[]>();

  constructor(table: Table) {
    this.#vectors = table.schema.fields.map((_, i) => table.getChildAt(i)!);
    this.#formats = columnText(table.schema);
  }

  /** Row `r`'s cells as text; NULL is null. */
  row(r: number): (string | null)[] {
    let row = this.#rows.get(r);
    if (!row) {
      row = this.#vectors.map((v, c) => this.#formats[c]!(v.get(r)));
      this.#rows.set(r, row);
    }
    return row;
  }
}
