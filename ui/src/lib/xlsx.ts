// An Excel workbook (.xlsx, one sheet) of a result's rows, written here: an .xlsx is a zip of a
// few XML files. Numbers stay numbers when Excel holds them exactly (integers up to 2^53,
// decimals of up to 15 digits), booleans stay booleans, everything else is text as the grid
// shows it (so `00123` keeps its zeros, and a 38-digit decimal its digits), NULL an empty cell.

import { type DataType, type Table, Type } from "apache-arrow";

import { jsonFor, textFor } from "./format";
import { zip } from "./zip";

const xmlHeader = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n';

/** Text for XML: escaped, and without the characters XML 1.0 can't hold. */
export function xmlText(text: string): string {
  return (
    text
      // eslint-disable-next-line no-control-regex
      .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f￾￿]/g, "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
  );
}

/** The column letters of a 0-based index: 0 → A, 25 → Z, 26 → AA. */
export function columnName(index: number): string {
  let n = index + 1;
  let out = "";
  while (n > 0) {
    const r = (n - 1) % 26;
    out = String.fromCharCode(65 + r) + out;
    n = Math.floor((n - 1) / 26);
  }
  return out;
}

type Cell =
  { kind: "number"; value: string } | { kind: "bool"; value: boolean } | { kind: "text"; value: string } | null;

/** How a value of `type` goes into a cell. */
function cellFor(type: DataType): (value: unknown) => Cell {
  const text = textFor(type);
  const json = jsonFor(type);
  switch (type.typeId) {
    case Type.Int:
    case Type.Float:
      return (v) => {
        if (v === null || v === undefined) return null;
        if (typeof v === "number")
          return Number.isFinite(v) ? { kind: "number", value: String(v) } : { kind: "text", value: text(v) ?? "" };
        const big = v as bigint;
        return big <= BigInt(Number.MAX_SAFE_INTEGER) && big >= -BigInt(Number.MAX_SAFE_INTEGER)
          ? { kind: "number", value: big.toString() }
          : { kind: "text", value: big.toString() };
      };
    case Type.Decimal:
      return (v) => {
        if (v === null || v === undefined) return null;
        const t = text(v) ?? "";
        return t.replace(/[-.]/g, "").replace(/^0+/, "").length <= 15
          ? { kind: "number", value: t }
          : { kind: "text", value: t };
      };
    case Type.Bool:
      return (v) => (v === null || v === undefined ? null : { kind: "bool", value: Boolean(v) });
    case Type.List:
    case Type.LargeList:
    case Type.FixedSizeList:
    case Type.Struct:
    case Type.Map:
      return (v) => (v === null || v === undefined ? null : { kind: "text", value: json(v) });
    default:
      return (v) => {
        const t = text(v);
        return t === null ? null : { kind: "text", value: t };
      };
  }
}

function cellXml(ref: string, cell: Cell, style = 0): string {
  if (cell === null) return "";
  const s = style ? ` s="${style}"` : "";
  switch (cell.kind) {
    case "number":
      return `<c r="${ref}"${s}><v>${cell.value}</v></c>`;
    case "bool":
      return `<c r="${ref}"${s} t="b"><v>${cell.value ? 1 : 0}</v></c>`;
    case "text":
      return `<c r="${ref}"${s} t="inlineStr"><is><t xml:space="preserve">${xmlText(cell.value)}</t></is></c>`;
  }
}

/** The sheet's XML: a bold header row, then the rows in `order` (all rows when undefined). */
export function sheetXml(table: Table, order?: readonly number[]): string {
  const fields = table.schema.fields;
  const vectors = fields.map((_, i) => table.getChildAt(i)!);
  const cells = fields.map((f) => cellFor(f.type as DataType));
  const rows: string[] = [];
  rows.push(
    `<row r="1">${fields.map((f, c) => cellXml(`${columnName(c)}1`, { kind: "text", value: f.name }, 1)).join("")}</row>`,
  );
  const count = order ? order.length : table.numRows;
  for (let p = 0; p < count; p++) {
    const r = order ? order[p]! : p;
    const n = p + 2;
    rows.push(
      `<row r="${n}">${vectors.map((v, c) => cellXml(`${columnName(c)}${n}`, cells[c]!(v.get(r)))).join("")}</row>`,
    );
  }
  return (
    xmlHeader +
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' +
    '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>' +
    `<sheetData>${rows.join("")}</sheetData></worksheet>`
  );
}

/** The workbook: one sheet, named `sheetName`, of the rows in `order`. */
export async function tableToXlsx(
  table: Table,
  options: { order?: readonly number[]; sheetName?: string } = {},
): Promise<Uint8Array> {
  const enc = new TextEncoder();
  const sheetName = xmlText((options.sheetName ?? "results").replace(/[\\/?*[\]:]/g, "_").slice(0, 31));
  const files: Record<string, string> = {
    "[Content_Types].xml":
      xmlHeader +
      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
      '<Default Extension="xml" ContentType="application/xml"/>' +
      '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
      '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' +
      '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' +
      "</Types>",
    "_rels/.rels":
      xmlHeader +
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
      '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>' +
      "</Relationships>",
    "xl/workbook.xml":
      xmlHeader +
      '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">' +
      `<sheets><sheet name="${sheetName}" sheetId="1" r:id="rId1"/></sheets></workbook>`,
    "xl/_rels/workbook.xml.rels":
      xmlHeader +
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
      '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>' +
      '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>' +
      "</Relationships>",
    "xl/styles.xml":
      xmlHeader +
      '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' +
      '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>' +
      '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>' +
      '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>' +
      '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>' +
      '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>' +
      '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>' +
      '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>' +
      "</styleSheet>",
    "xl/worksheets/sheet1.xml": sheetXml(table, options.order),
  };
  return zip(Object.entries(files).map(([path, text]) => ({ path, data: enc.encode(text) })));
}
