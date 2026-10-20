// Test workbooks written by hand: an .xlsx is a zip of SpreadsheetML parts, so each case
// (shared and inline strings, a formula's saved value, a merged range, the 1904 date system,
// date formats Excel writes) is in the file exactly as Excel would put it, not as the library
// under test would write it. Shared by the unit tests and the e2e tests.

import { zip } from "../../src/lib/zip";

/** A cell: a number (with a style: see `styles`), shared or inline text, a boolean, an error, a formula. */
export type XCell =
  | { n: number; style?: keyof typeof styles }
  | { s: string }
  | { is: string }
  | { b: boolean }
  | { e: string }
  | { f: string; v?: number | string };

export interface SheetSpec {
  name: string;
  /** Rows from row 1 (null: an empty row); cells from column A (null: an empty cell). */
  rows: ((XCell | null)[] | null)[];
  merges?: string[];
}

/** The cell styles the sheets use, by name: the index into cellXfs and the number format. */
export const styles = {
  general: 0,
  /** Built-in 14: m/d/yyyy */
  date: 1,
  /** Built-in 22: m/d/yy h:mm */
  datetime: 2,
  /** Built-in 21: h:mm:ss */
  time: 3,
  /** Custom 164: yyyy-mm-dd */
  isoDate: 4,
  /** Built-in 4: #,##0.00 */
  money: 5,
} as const;

const enc = new TextEncoder();
const esc = (s: string) =>
  s.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");

function letters(i: number): string {
  let n = i + 1;
  let out = "";
  while (n > 0) {
    const r = (n - 1) % 26;
    out = String.fromCharCode(65 + r) + out;
    n = Math.floor((n - 1) / 26);
  }
  return out;
}

/** An .xlsx of `sheets`; `date1904` sets the workbook's date system. */
export async function buildXlsx(sheets: SheetSpec[], options: { date1904?: boolean } = {}): Promise<Uint8Array> {
  const shared: string[] = [];
  const sharedIndex = new Map<string, number>();
  const sst = (s: string) => {
    let i = sharedIndex.get(s);
    if (i === undefined) {
      i = shared.length;
      shared.push(s);
      sharedIndex.set(s, i);
    }
    return i;
  };
  const sheetXml = sheets.map((sheet) => {
    const rows = sheet.rows
      .map((row, r) => {
        if (!row) return "";
        const cells = row
          .map((cell, c) => {
            if (!cell) return "";
            const ref = `${letters(c)}${r + 1}`;
            if ("n" in cell) return `<c r="${ref}" s="${styles[cell.style ?? "general"]}"><v>${cell.n}</v></c>`;
            if ("s" in cell) return `<c r="${ref}" t="s"><v>${sst(cell.s)}</v></c>`;
            if ("is" in cell) return `<c r="${ref}" t="inlineStr"><is><t>${esc(cell.is)}</t></is></c>`;
            if ("b" in cell) return `<c r="${ref}" t="b"><v>${cell.b ? 1 : 0}</v></c>`;
            if ("e" in cell) return `<c r="${ref}" t="e"><v>${esc(cell.e)}</v></c>`;
            const t = typeof cell.v === "string" ? ' t="str"' : "";
            const v = cell.v === undefined ? "" : `<v>${esc(String(cell.v))}</v>`;
            return `<c r="${ref}"${t}><f>${esc(cell.f)}</f>${v}</c>`;
          })
          .join("");
        return `<row r="${r + 1}">${cells}</row>`;
      })
      .join("");
    const merges = sheet.merges?.length
      ? `<mergeCells count="${sheet.merges.length}">${sheet.merges.map((m) => `<mergeCell ref="${m}"/>`).join("")}</mergeCells>`
      : "";
    return (
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' +
      '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' +
      `<sheetData>${rows}</sheetData>${merges}</worksheet>`
    );
  });
  const ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"';
  const rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
  const files: Record<string, string> = {
    "[Content_Types].xml":
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' +
      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
      '<Default Extension="xml" ContentType="application/xml"/>' +
      '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
      sheets
        .map(
          (_, i) =>
            `<Override PartName="/xl/worksheets/sheet${i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>`,
        )
        .join("") +
      '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' +
      '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>' +
      "</Types>",
    "_rels/.rels":
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' +
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
      `<Relationship Id="rId1" Type="${rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>`,
    "xl/workbook.xml":
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' +
      `<workbook ${ns} xmlns:r="${rel}">` +
      (options.date1904 ? '<workbookPr date1904="1"/>' : "<workbookPr/>") +
      `<sheets>${sheets.map((s, i) => `<sheet name="${esc(s.name)}" sheetId="${i + 1}" r:id="rId${i + 1}"/>`).join("")}</sheets>` +
      "</workbook>",
    "xl/_rels/workbook.xml.rels":
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' +
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
      sheets
        .map((_, i) => `<Relationship Id="rId${i + 1}" Type="${rel}/worksheet" Target="worksheets/sheet${i + 1}.xml"/>`)
        .join("") +
      `<Relationship Id="rId${sheets.length + 1}" Type="${rel}/styles" Target="styles.xml"/>` +
      `<Relationship Id="rId${sheets.length + 2}" Type="${rel}/sharedStrings" Target="sharedStrings.xml"/>` +
      "</Relationships>",
    "xl/styles.xml":
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' +
      `<styleSheet ${ns}>` +
      '<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd"/></numFmts>' +
      '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>' +
      '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>' +
      '<borders count="1"><border/></borders>' +
      '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>' +
      '<cellXfs count="6">' +
      '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>' +
      '<xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>' +
      '<xf numFmtId="22" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>' +
      '<xf numFmtId="21" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>' +
      '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>' +
      '<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>' +
      "</cellXfs></styleSheet>",
  };
  sheetXml.forEach((xml, i) => (files[`xl/worksheets/sheet${i + 1}.xml`] = xml));
  files["xl/sharedStrings.xml"] =
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' +
    `<sst ${ns} count="${shared.length}" uniqueCount="${shared.length}">` +
    shared.map((s) => `<si><t xml:space="preserve">${esc(s)}</t></si>`).join("") +
    "</sst>";
  return zip(
    Object.entries(files).map(([path, text]) => ({ path, data: enc.encode(text) })),
    { compress: false, now: new Date(2026, 0, 1) },
  );
}

/** Excel's serial number of a date (1900 system, or 1904), with the time as a fraction. */
export function serial(y: number, m: number, d: number, h = 0, mi = 0, s = 0, epoch1904 = false): number {
  const days = Date.UTC(y, m - 1, d) / 86_400_000;
  const base = epoch1904 ? Date.UTC(1904, 0, 1) / 86_400_000 : Date.UTC(1899, 11, 30) / 86_400_000;
  return days - base + (h * 3600 + mi * 60 + s) / 86400;
}
