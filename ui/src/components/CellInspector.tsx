import { Check, Copy, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { copyText } from "../lib/clipboard";
import { looksLikeJson, prettyJson } from "../lib/prettyJson";

export interface InspectedCell {
  column: string;
  type: string;
  /** The row's number as shown (1-based, after the filter and the sort). */
  row: number;
  /** The grid's text of the value; null for SQL NULL. */
  text: string | null;
  /** A nested value (list, struct, map) as exact JSON. */
  json?: string | undefined;
  raw?: string | undefined;
}

/**
 * One value in full: the grid shortens long text and puts nested values on one line. JSON (a
 * nested value, or text holding a JSON object or array) is pretty-printed, every number exact.
 * Copy value copies what is shown; Esc closes, and the grid keeps its selection.
 */
export function CellInspector({ cell, onClose }: { cell: InspectedCell; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  const { text, json } = useMemo(() => {
    if (cell.text === null) return { text: "NULL", json: false };
    if (cell.json !== undefined) return { text: prettyJson(cell.json), json: true };
    if (looksLikeJson(cell.text)) return { text: prettyJson(cell.text), json: true };
    return { text: cell.text, json: false };
  }, [cell]);
  return (
    <dialog
      ref={dialog}
      className="dialog dialog-wide"
      onClose={onClose}
      aria-label={`Value of ${cell.column}`}
      data-testid="cell-inspector"
    >
      <div className="dialog-head">
        <h2 className="mono">
          {cell.column} <span className="inspector-type">{cell.type}</span>
        </h2>
        <button type="button" className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close">
          <X size={16} />
        </button>
      </div>
      <p className="inspector-meta">
        Row {cell.row.toLocaleString()} ·{" "}
        {cell.text === null ? "NULL" : `${cell.text.length.toLocaleString()} characters`}
        {json ? " · JSON" : ""}
      </p>
      <pre className={`value-text${cell.text === null ? " grid-null" : ""}`} data-testid="inspector-value">
        {text}
      </pre>
      {cell.raw !== undefined && (
        <details>
          <summary>Raw Arrow storage</summary>
          <pre className="value-text">{prettyJson(cell.raw)}</pre>
        </details>
      )}
      <div className="dialog-foot">
        <button
          type="button"
          className="btn btn-secondary"
          disabled={cell.text === null}
          onClick={() => void copyText(text).then((ok) => setCopied(ok))}
        >
          {copied ? <Check size={14} /> : <Copy size={14} />} {copied ? "Copied" : "Copy value"}
        </button>
      </div>
    </dialog>
  );
}
