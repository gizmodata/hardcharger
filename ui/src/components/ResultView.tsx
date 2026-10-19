import type { Table } from "apache-arrow";
import { Copy, Download, Maximize2, Minimize2 } from "lucide-react";
import { useEffect, useState } from "react";

import { copyText } from "../lib/clipboard";
import { exportName, saveFile } from "../lib/download";
import { type ExportFormat, exportFormats, exportTable, toDelimited } from "../lib/export";
import { errorMessage } from "../session";
import { FloatingMenu } from "./FloatingMenu";
import { ResultsGrid } from "./ResultsGrid";

const plural = (n: number, word: string) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;

interface Props {
  table: Table;
  /** The row limit, when the result was cut off at it. */
  truncatedAt?: number | undefined;
  /** Expand: the results fill the page (the editor folds away); Restore brings it back. */
  expanded?: boolean;
  onExpand?: (expanded: boolean) => void;
}

/** One result set: the grid, with its toolbar (copy, export, expand). */
export function ResultView({ table, truncatedAt, expanded = false, onExpand }: Props) {
  const [notice, setNotice] = useState<string | undefined>();
  const [exportMenu, setExportMenu] = useState<{ x: number; y: number; shown: readonly number[] } | undefined>();
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    if (!notice) return;
    const t = window.setTimeout(() => setNotice(undefined), 2500);
    return () => window.clearTimeout(t);
  }, [notice]);

  const doExport = async (format: ExportFormat, shown: readonly number[]) => {
    setExporting(true);
    const info = exportFormats.find((f) => f.format === format)!;
    try {
      const blob = await exportTable(table, format, info.scope === "shown" ? shown : undefined);
      saveFile(blob, exportName(info.extension));
      const rows = info.scope === "shown" ? shown.length : table.numRows;
      setNotice(`Exported ${plural(rows, "row")} as ${format.toUpperCase()}.`);
    } catch (err) {
      console.info("export failed:", err);
      setNotice(`The ${format.toUpperCase()} export failed: ${errorMessage(err)}`);
    } finally {
      setExporting(false);
    }
  };

  const copyShown = async (order: readonly number[]) => {
    const ok = await copyText(toDelimited(table, "\t", { order }));
    setNotice(ok ? `Copied ${plural(order.length, "row")} with headers.` : "The browser refused the copy.");
  };

  return (
    <div className="result-view">
      <ResultsGrid
        table={table}
        onNotice={setNotice}
        toolbar={(shown) => (
          <>
            {truncatedAt !== undefined && (
              <span className="badge" data-testid="truncated">
                truncated at {truncatedAt.toLocaleString()} rows
              </span>
            )}
            {notice && (
              <span className="results-notice" role="status" data-testid="results-notice">
                {notice}
              </span>
            )}
            {onExpand && (
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => onExpand(!expanded)}
                data-testid="results-expand"
                title={expanded ? "Back to the editor and the results" : "Fill the page with the results"}
              >
                {expanded ? <Minimize2 size={14} /> : <Maximize2 size={14} />} {expanded ? "Restore" : "Expand"}
              </button>
            )}
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => void copyShown(shown)}
              title="Copy every row shown (filtered and sorted), with headers, as tab-separated text"
            >
              <Copy size={14} /> Copy
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={(e) => {
                const r = e.currentTarget.getBoundingClientRect();
                setExportMenu({ x: r.right, y: r.bottom + 4, shown });
              }}
              disabled={exporting}
              aria-haspopup="menu"
              aria-expanded={!!exportMenu}
              data-testid="export-button"
            >
              <Download size={14} /> {exporting ? "Downloading…" : "Download"}
            </button>
            {exportMenu && (
              <FloatingMenu
                x={exportMenu.x}
                y={exportMenu.y}
                align="right"
                onClose={() => setExportMenu(undefined)}
                items={[
                  {
                    label: `As shown (${exportMenu.shown.length === table.numRows ? "all rows" : plural(exportMenu.shown.length, "shown row")})`,
                    heading: true,
                  },
                  ...exportFormats
                    .filter((f) => f.scope === "shown")
                    .map((f) => ({ label: f.label, run: () => void doExport(f.format, exportMenu.shown) })),
                  { label: `Full result, typed (${plural(table.numRows, "row")})`, heading: true },
                  ...exportFormats
                    .filter((f) => f.scope === "full")
                    .map((f) => ({ label: f.label, run: () => void doExport(f.format, exportMenu.shown) })),
                ]}
              />
            )}
          </>
        )}
      />
    </div>
  );
}
