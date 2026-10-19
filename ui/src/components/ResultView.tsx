import type { Table } from "apache-arrow";
import { Copy, Download } from "lucide-react";
import { useEffect, useState } from "react";

import { copyText } from "../lib/clipboard";
import { exportName, saveFile } from "../lib/download";
import { type ExportFormat, exportFormats, exportTable, toDelimited } from "../lib/export";
import { errorMessage } from "../session";
import { ResultsGrid } from "./ResultsGrid";

const plural = (n: number, word: string) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;

interface Props {
  table: Table;
  /** The row limit, when the result was cut off at it. */
  truncatedAt?: number | undefined;
}

/** One result set: its toolbar (copy, export) and the grid. */
export function ResultView({ table, truncatedAt }: Props) {
  const [notice, setNotice] = useState<string | undefined>();
  const [exportOpen, setExportOpen] = useState(false);
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    if (!notice) return;
    const t = window.setTimeout(() => setNotice(undefined), 2500);
    return () => window.clearTimeout(t);
  }, [notice]);

  const doExport = async (format: ExportFormat) => {
    setExportOpen(false);
    setExporting(true);
    try {
      const blob = await exportTable(table, format);
      saveFile(blob, exportName(exportFormats.find((f) => f.format === format)!.extension));
      setNotice(`Exported ${plural(table.numRows, "row")} as ${format.toUpperCase()}.`);
    } catch (err) {
      console.info("export failed:", err);
      setNotice(`The ${format.toUpperCase()} export failed: ${errorMessage(err)}`);
    } finally {
      setExporting(false);
    }
  };

  const copyAll = async () => {
    const ok = await copyText(toDelimited(table, "\t"));
    setNotice(ok ? `Copied ${plural(table.numRows, "row")} with headers.` : "The browser refused the copy.");
  };

  return (
    <div className="result-view">
      <div className="result-toolbar">
        <span className="results-count">{plural(table.numRows, "row")}</span>
        {truncatedAt !== undefined && (
          <span className="badge" data-testid="truncated">
            truncated at {truncatedAt.toLocaleString()} rows
          </span>
        )}
        <span className="toolbar-spacer" />
        {notice && (
          <span className="results-notice" role="status" data-testid="results-notice">
            {notice}
          </span>
        )}
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => void copyAll()}
          title="Copy every row, with headers, as tab-separated text"
        >
          <Copy size={14} /> Copy
        </button>
        <div className="menu-wrap">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => setExportOpen((o) => !o)}
            disabled={exporting}
            aria-haspopup="menu"
            aria-expanded={exportOpen}
            data-testid="export-button"
          >
            <Download size={14} /> {exporting ? "Exporting…" : "Export"}
          </button>
          {exportOpen && (
            <div className="menu" role="menu" onMouseLeave={() => setExportOpen(false)}>
              {exportFormats.map((f) => (
                <button key={f.format} type="button" role="menuitem" onClick={() => void doExport(f.format)}>
                  {f.label}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
      <div className="result-grid-wrap">
        <ResultsGrid
          table={table}
          onCopied={(n) => setNotice(n === false ? "The browser refused the copy." : `Copied ${plural(n, "cell")}.`)}
        />
      </div>
    </div>
  );
}
