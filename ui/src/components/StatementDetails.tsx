import { Copy } from "lucide-react";
import { useState, type ReactNode } from "react";

import { copyText } from "../lib/clipboard";
import { statusLabel, type MonitoredQuery } from "../lib/sqlMonitor";
import { QueryProfile } from "./QueryProfile";

const timestamp = (ms: number | null) => (ms == null ? "—" : new Date(ms).toLocaleString());

export function StatementDetails({ query, actions }: { query: MonitoredQuery; actions?: ReactNode }) {
  const [copied, setCopied] = useState<{ executionId: string; message: string }>();
  return (
    <section className="monitor-detail" aria-label="Statement details">
      <div className="page-head">
        <h3>{statusLabel(query)}</h3>
        {actions}
      </div>
      <div className="monitor-sql-block">
        <div className="monitor-sql-head">
          <span role="status">{copied?.executionId === query.execution_id ? copied.message : "SQL"}</span>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() =>
              void copyText(query.sql_text).then((ok) =>
                setCopied({
                  executionId: query.execution_id,
                  message: ok ? "Copied to clipboard." : "Copy failed; select the SQL and copy it with your keyboard.",
                }),
              )
            }
          >
            <Copy size={14} /> Copy SQL
          </button>
        </div>
        <pre className="value-text" tabIndex={0} aria-label="SQL text">
          {query.sql_text}
        </pre>
      </div>
      <dl>
        <dt>Execution / statement</dt>
        <dd className="mono">
          {query.execution_id} / {query.statement_id}
        </dd>
        <dt>Session / user</dt>
        <dd>
          {query.session_id} / {query.username}
        </dd>
        <dt>Enqueued</dt>
        <dd>{timestamp(query.enqueue_time)}</dd>
        <dt>Execution started / produced results</dt>
        <dd>
          {timestamp(query.execution_start_time)} / {timestamp(query.execution_end_time)}
        </dd>
        <dt>Fetch started / ended</dt>
        <dd>
          {timestamp(query.fetch_start_time)} / {timestamp(query.fetch_end_time)}
        </dd>
        <dt>Cursor closed</dt>
        <dd>{timestamp(query.cursor_close_time)}</dd>
        <dt>Cancellation reason</dt>
        <dd>{query.cancel_reason || "—"}</dd>
      </dl>
      {query.error_message && <pre className="result-error">{query.error_message}</pre>}
      <QueryProfile key={query.execution_id} executionId={query.execution_id} />
    </section>
  );
}
