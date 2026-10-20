// The file formats an upload reads, how a file's format is told, and the sizes it may have.

export type UploadFormat = "csv" | "tsv" | "xlsx" | "xls" | "xlsb" | "parquet" | "json" | "arrow";

/** What the file picker offers: GizmoData Cloud's list, and the older Excel formats. */
export const acceptedExtensions =
  ".csv,.tsv,.tab,.txt,.xlsx,.xls,.xlsb,.parquet,.pq,.json,.ndjson,.jsonl,.arrow,.arrows,.feather,.ipc";

const byExtension: Record<string, UploadFormat> = {
  csv: "csv",
  txt: "csv",
  tsv: "tsv",
  tab: "tsv",
  xlsx: "xlsx",
  xls: "xls",
  xlsb: "xlsb",
  parquet: "parquet",
  pq: "parquet",
  json: "json",
  ndjson: "json",
  jsonl: "json",
  arrow: "arrow",
  arrows: "arrow",
  feather: "arrow",
  ipc: "arrow",
};

export function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot + 1).toLowerCase();
}

/** The format a file name says, if it says one. */
export function formatFromName(name: string): UploadFormat | undefined {
  return byExtension[extensionOf(name)];
}

/** The format from a file's first bytes (for a name that says none), as GizmoData Cloud tells it. */
export function formatFromBytes(head: Uint8Array): UploadFormat {
  const starts = (...bytes: number[]) => bytes.every((b, i) => head[i] === b);
  if (starts(0x50, 0x41, 0x52, 0x31)) return "parquet"; // PAR1
  if (starts(0x41, 0x52, 0x52, 0x4f, 0x57, 0x31) || starts(0xff, 0xff, 0xff, 0xff)) return "arrow";
  if (starts(0x50, 0x4b, 0x03, 0x04)) return "xlsx";
  if (starts(0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1)) return "xls";
  let i = 0;
  if (starts(0xef, 0xbb, 0xbf)) i = 3;
  while (i < head.length && (head[i] === 0x20 || head[i] === 0x09 || head[i] === 0x0a || head[i] === 0x0d)) i++;
  if (head[i] === 0x7b || head[i] === 0x5b) return "json";
  return "csv";
}

export const formatLabels: Record<UploadFormat, string> = {
  csv: "CSV",
  tsv: "TSV",
  xlsx: "Excel",
  xls: "Excel 97-2003",
  xlsb: "Excel binary",
  parquet: "Parquet",
  json: "JSON",
  arrow: "Arrow IPC",
};

export function isExcel(format: UploadFormat | undefined): boolean {
  return format === "xlsx" || format === "xls" || format === "xlsb";
}

export function isDelimited(format: UploadFormat | undefined): boolean {
  return format === "csv" || format === "tsv";
}

/**
 * Files read whole in the browser, and their limit: a workbook and a JSON array are parsed in
 * one piece (an .xlsx's XML is many times its zipped size in memory), an Arrow IPC file is
 * read into memory to find its batches. The other formats are read as they upload, in pieces
 * of a few megabytes, so their size has no limit here (the server's disk is the limit).
 */
export const wholeFileLimits = {
  excel: 100 * 1024 * 1024,
  json: 100 * 1024 * 1024,
  arrowFile: 1024 * 1024 * 1024,
};

/** The limit for this file, if it is read whole; undefined when it streams. */
export function wholeFileLimit(format: UploadFormat, jsonArray: boolean, arrowFile: boolean): number | undefined {
  if (isExcel(format)) return wholeFileLimits.excel;
  if (format === "json" && jsonArray) return wholeFileLimits.json;
  if (format === "arrow" && arrowFile) return wholeFileLimits.arrowFile;
  return undefined;
}

/** `Sales Q3 2026.xlsx` → `sales_q3_2026`, as GizmoData Cloud derives a table's name. */
export function defaultTableName(fileName: string): string {
  const stem = fileName
    .replace(/\.[^./\\]*$/, "")
    .trim()
    .toLowerCase();
  let cleaned = stem
    .replace(/[^a-z0-9]/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_+|_+$/g, "");
  if (!cleaned) return "uploaded_data";
  if (/^[0-9]/.test(cleaned)) cleaned = `t_${cleaned}`;
  return cleaned;
}

export const maxIdentifierLength = 128;

/** Why a table or schema name can't be used, or undefined when it can. */
export function nameProblem(value: string, what: "table" | "schema"): string | undefined {
  const name = value.trim();
  if (!name) return `A ${what} name is required.`;
  if (name.length > maxIdentifierLength || name.includes("\u0000")) {
    return `The ${what} name must be 1–${maxIdentifierLength} characters.`;
  }
  return undefined;
}

/** 1.5 MB, 120 KB, as GizmoData Cloud writes sizes. */
export function formatBytes(n: number): string {
  const units = ["B", "KB", "MB", "GB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${i === 0 || Number.isInteger(v) ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
}
