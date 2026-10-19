// Result exports: CSV, TSV and JSON from the Arrow table the grid shows; Parquet through
// parquet-wasm (loaded only when asked for, see parquet.ts).

import type { DataType, Table } from "apache-arrow";

import { columnText, jsonFor } from "./format";

export type ExportFormat = "csv" | "tsv" | "json" | "parquet";

export const exportFormats: { format: ExportFormat; label: string; extension: string; type: string }[] = [
  { format: "csv", label: "CSV (comma-separated)", extension: "csv", type: "text/csv" },
  { format: "tsv", label: "TSV (tab-separated)", extension: "tsv", type: "text/tab-separated-values" },
  { format: "json", label: "JSON", extension: "json", type: "application/json" },
  { format: "parquet", label: "Parquet", extension: "parquet", type: "application/vnd.apache.parquet" },
];

/**
 * One field of delimited text: quoted (RFC 4180 style) when it holds the delimiter, a quote,
 * a line break or surrounding spaces. Spreadsheets read the same quoting in tab-separated
 * text, so TSV and the clipboard use it too.
 */
export function delimitedField(text: string, delimiter: string): string {
  if (
    text.includes(delimiter) ||
    text.includes('"') ||
    text.includes("\n") ||
    text.includes("\r") ||
    text !== text.trim()
  ) {
    return `"${text.replaceAll('"', '""')}"`;
  }
  return text;
}

/**
 * Rows of a table as delimited text; NULL is an empty field. Lines end with \n. `order` picks
 * the rows and their order (the grid's filter and sort); `rows` a range of them.
 */
export function toDelimited(
  table: Table,
  delimiter: string,
  options: {
    header?: boolean;
    rows?: [number, number];
    columns?: [number, number];
    order?: readonly number[];
  } = {},
): string {
  const order = options.order;
  const [r0, r1] = options.rows ?? [0, (order ? order.length : table.numRows) - 1];
  const [c0, c1] = options.columns ?? [0, table.numCols - 1];
  const text = columnText(table.schema);
  const vectors = table.schema.fields.map((_, i) => table.getChildAt(i)!);
  const lines: string[] = [];
  if (options.header ?? true) {
    lines.push(
      table.schema.fields
        .slice(c0, c1 + 1)
        .map((f) => delimitedField(f.name, delimiter))
        .join(delimiter),
    );
  }
  for (let p = r0; p <= r1; p++) {
    const r = order ? order[p]! : p;
    const fields: string[] = [];
    for (let c = c0; c <= c1; c++) fields.push(delimitedField(text[c]!(vectors[c]!.get(r)) ?? "", delimiter));
    lines.push(fields.join(delimiter));
  }
  return lines.length ? `${lines.join("\n")}\n` : "";
}

/** The rows as a JSON array of objects, with exact numbers. */
export function toJson(table: Table): string {
  const json = table.schema.fields.map((f) => jsonFor(f.type as DataType));
  const keys = table.schema.fields.map((f) => JSON.stringify(f.name));
  const vectors = table.schema.fields.map((_, i) => table.getChildAt(i)!);
  const rows: string[] = [];
  for (let r = 0; r < table.numRows; r++) {
    rows.push(`{${keys.map((k, c) => `${k}:${json[c]!(vectors[c]!.get(r))}`).join(",")}}`);
  }
  return rows.length ? `[\n${rows.join(",\n")}\n]\n` : "[]\n";
}

/** A file of the table in `format`. */
export async function exportTable(table: Table, format: ExportFormat): Promise<Blob> {
  const meta = exportFormats.find((f) => f.format === format)!;
  switch (format) {
    case "csv":
      return new Blob([toDelimited(table, ",")], { type: meta.type });
    case "tsv":
      return new Blob([toDelimited(table, "\t")], { type: meta.type });
    case "json":
      return new Blob([toJson(table)], { type: meta.type });
    case "parquet": {
      const { tableToParquet } = await import("./parquet");
      const bytes = await tableToParquet(table);
      return new Blob([bytes], { type: meta.type });
    }
  }
}
