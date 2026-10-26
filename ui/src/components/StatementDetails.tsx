import type { ReactNode } from "react";

import { statusLabel, type MonitoredQuery } from "../lib/sqlMonitor";
import { QueryProfile } from "./QueryProfile";

const timestamp = (ms: number | null) => (ms == null ? "—" : new Date(ms).toLocaleString());

export function StatementDetails({ query, actions }: { query: MonitoredQuery; actions?: ReactNode }) {
  return (
    <section className="monitor-detail" aria-label="Statement details">
      <div className="page-head">
        <h3>{statusLabel(query)}</h3>
        {actions}
      </div>
      <pre className="value-text">{query.sql_text}</pre>
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
