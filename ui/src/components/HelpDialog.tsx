import { X } from "lucide-react";
import { useEffect, useRef } from "react";

import { idleTimeoutText } from "../lib/format";

/**
 * How the editor works, in short: sessions, transactions, statements, results, and what this
 * browser keeps. `idleTimeoutS` is this server's session idle timeout (the session document's
 * idle_timeout_s), unknown until the first session document arrives.
 */
export function HelpDialog({ idleTimeoutS, onClose }: { idleTimeoutS: number | undefined; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  const idle =
    idleTimeoutS === undefined
      ? "after it has been idle for as long as the server allows"
      : idleTimeoutS === 0
        ? "never for being idle (this server keeps idle sessions)"
        : `after ${idleTimeoutText(idleTimeoutS)} idle (this server's setting)`;
  return (
    <dialog ref={dialog} className="dialog dialog-help" onClose={onClose} aria-labelledby="help-title">
      <div className="dialog-head">
        <h2 id="help-title">How the editor works</h2>
        <button type="button" className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close">
          <X size={16} />
        </button>
      </div>
      <div className="help-body" data-testid="help-body">
        <h3>Sessions</h3>
        <p>
          Each tab has its own session on the server, kept from one run to the next: temporary tables, <code>USE</code>{" "}
          and <code>SET</code> carry over. <strong>New session</strong> ends it, and closing the tab does too, as does
          closing, reloading or leaving this page (the browser asks first while a transaction is open). A session also
          ends {idle}, or when the server restarts. If no transaction was open, the next run starts a new session and
          says so.
        </p>
        <h3>Connections</h3>
        <p>
          Use Connection in the toolbar to move an existing tab to another connected server. Your SQL, tab name and row
          limit stay with the tab. Its old session ends and its results clear; the next run uses a fresh session and the
          new connection&apos;s default catalog, schema and roles. Finish or cancel a running query, and commit or roll
          back an open transaction, before switching.
        </p>
        <h3>Transactions</h3>
        <p>
          Every statement commits as it runs, until you run <code>BEGIN</code>. The transaction then stays open across
          runs until <code>COMMIT</code> or <code>ROLLBACK</code> (or the buttons beside the badge), and Catalog, Schema
          and Run as are locked. After a failed statement the server refuses everything until you roll back. If the
          session ends while a transaction is open, the next run is refused and nothing from it executes.
        </p>
        <h3>Statements</h3>
        <p>
          Ctrl/⌘+Enter runs the statement at the cursor (the bar in the margin marks it) or exactly the selection.
          Shift+Ctrl/⌘+Enter runs them all in order and stops at the first error. Cancel stops the statement on the
          server; nothing of it is saved.
        </p>
        <h3>Format SQL</h3>
        <p>
          Right-click in the editor and choose Format SQL, or press Alt+Shift+F. It formats the selection, or the whole
          script when nothing is selected. Undo restores the previous text. The style uses leading commas, spaces,
          lowercase unquoted identifiers and uppercase keywords. Quoted names and string values retain their spelling.
          SQL details also offer Formatted and Original views.
        </p>
        <h3>Results</h3>
        <p>
          Up to the row limit. Click a header to sort (NULLs last), type to filter, select cells like a spreadsheet and
          copy them with Ctrl/⌘+C. CSV, TSV, JSON and Excel downloads hold the rows as shown; Parquet and Arrow the
          whole result with its exact types.
        </p>
        <h3>Kept in this browser</h3>
        <p>
          Saved queries and your open tabs (with their text) stay in this browser, kept for each user of this server,
          and so do the sizes you give the side panel and the editor. None of it is stored on the server, and it
          doesn&apos;t follow you to another browser. To move saved queries, use <strong>Export all to a file</strong>{" "}
          in the Saved queries menu, then <strong>Import from a file</strong> in the other browser.
        </p>
      </div>
    </dialog>
  );
}
