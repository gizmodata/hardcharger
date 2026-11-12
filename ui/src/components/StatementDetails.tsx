import { type ReactNode } from "react";

import { SqlText } from "./SqlText";
import { queryTimings, timingElapsed, timingTimestamp } from "../lib/queryTiming";
import { statusLabel, type MonitoredQuery } from "../lib/sqlMonitor";
import { QueryProfile } from "./QueryProfile";

export function StatementDetails({
  query,
  now,
  actions,
  server = "",
}: {
  query: MonitoredQuery;
  now: number;
  actions?: ReactNode;
  server?: string;
}) {
  return (
    <section className="monitor-detail" aria-label="Statement details">
      <div className="page-head">
        <h3>{statusLabel(query)}</h3>
        {actions}
      </div>
      <SqlText key={query.execution_id} sql={query.sql_text} />
      <dl>
        <dt>Execution / statement</dt>
        <dd className="mono">
          {query.execution_id} / {query.statement_id}
        </dd>
        <dt>Session / user</dt>
        <dd>
          {query.session_id} / {query.username}
        </dd>
        <dt>Cancellation reason</dt>
        <dd>{query.cancel_reason || "—"}</dd>
        <dt>Query tags</dt>
        <dd className="mono">{query.query_tag || "—"}</dd>
      </dl>
      <div className="query-timings-scroll">
        <table className="query-timings" aria-label="Query step timings">
          <caption>Timing · local time</caption>
          <thead>
            <tr>
              <th scope="col">Step</th>
              <th scope="col">Started</th>
              <th scope="col">Ended</th>
              <th scope="col">Elapsed</th>
            </tr>
          </thead>
          <tbody>
            {queryTimings(query, now).map((step) => (
              <tr key={step.id} data-testid={`query-timing-${step.id}`}>
                <th scope="row">{step.label}</th>
                <td>{timingTimestamp(step.start)}</td>
                <td title={step.end === null ? undefined : timingTimestamp(step.end)}>
                  {step.end !== null ? timingTimestamp(step.end, step.start) : step.live ? "In progress" : step.empty}
                </td>
                <td className="mono">
                  {timingElapsed(step.elapsed)}
                  {step.live && <small className="query-timing-live"> so far</small>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="page-sub">
        Execution ends when the result cursor is ready; fetch setup includes producing the first batch. Total runs from
        enqueue or execution start through cursor close, rather than measuring client download time.
      </p>
      {query.error_message && <pre className="result-error">{query.error_message}</pre>}
      <QueryProfile key={`${server}:${query.execution_id}`} executionId={query.execution_id} server={server} />
    </section>
  );
}
