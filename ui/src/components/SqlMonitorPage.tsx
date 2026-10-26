import { Activity, Pause, Play, RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { useAdminConnection } from "../lib/adminConnection";
import { activeQuery, monitorSql, statusLabel, type MonitorInfo, type MonitoredQuery } from "../lib/sqlMonitor";
import { errorMessage, useSession } from "../session";

const timestamp = (ms: number | null) => (ms == null ? "—" : new Date(ms).toLocaleString());
const duration = (ms: number) =>
  ms < 1000 ? `${Math.max(0, Math.round(ms))} ms` : `${(Math.max(0, ms) / 1000).toFixed(1)} s`;

export default function SqlMonitorPage() {
  const session = useSession();
  const { connection, error: connectionError, allowed } = useAdminConnection("sql-monitor");
  const [minutes, setMinutes] = useState(10);
  const [historyLimit, setHistoryLimit] = useState(1000);
  const [paused, setPaused] = useState(false);
  const [includeMonitor, setIncludeMonitor] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [info, setInfo] = useState<MonitorInfo>();
  const [queries, setQueries] = useState<MonitoredQuery[]>();
  const [error, setError] = useState<string>();
  const [updated, setUpdated] = useState<number>();
  const [selected, setSelected] = useState<string>();
  const [filter, setFilter] = useState("");
  const [state, setState] = useState("all");

  const sampling = useRef(Promise.resolve());
  useEffect(() => {
    if (!connection) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const table = await connection.query("SELECT * FROM hardcharger_monitor_info()");
        const next = table.get(0)?.toJSON() as unknown as MonitorInfo;
        if (!live) return;
        setInfo(next);
        if (next.instrumentation_enabled) {
          const location = await connection.query(
            "SELECT HARDCHARGER_INSTRUMENTATION_CATALOG() AS catalog, HARDCHARGER_INSTRUMENTATION_SCHEMA() AS schema",
          );
          if (!live) return;
          const at = location.get(0)!.toJSON() as { catalog: string; schema: string };
          const result = await connection.query(
            monitorSql(at.catalog, at.schema, minutes, includeMonitor, historyLimit),
          );
          if (!live) return;
          setQueries([...result].map((r) => (r as unknown as { toJSON: () => MonitoredQuery }).toJSON()));
        }
        setError(undefined);
        setUpdated(Date.now());
      } catch (err) {
        if (live && !session.checkSignIn(err)) setError(errorMessage(err));
      } finally {
        // Schedule after completion: a slow sample never overlaps another poll.
        if (live && !paused) timer = setTimeout(load, 2000);
      }
    };
    const load = () => {
      sampling.current = sampling.current.then(async () => {
        if (live) await poll();
      });
    };
    load();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [connection, session, minutes, includeMonitor, paused, refresh, historyLimit]);

  if (!allowed)
    return (
      <div className="sessions-page">
        <h2>SQL Monitor</h2>
        <p>Only unrestricted administrators can view SQL Monitor.</p>
      </div>
    );
  const disabled = info?.instrumentation_enabled === false || session.identity.instrumentationEnabled === false;
  const rows = (queries ?? []).filter(
    (q) =>
      (state === "all" ||
        (state === "active"
          ? activeQuery(q)
          : state === "timeout"
            ? q.status === "timeout" || q.cancel_reason === "timeout" || q.cancel_reason === "queue_timeout"
            : q.status === state)) &&
      `${q.sql_text} ${q.username} ${q.execution_id}`.toLowerCase().includes(filter.toLowerCase()),
  );
  const inspected = queries?.find((q) => q.execution_id === selected);
  const now = updated ?? 0;
  const terminalCount = queries?.filter((q) => !activeQuery(q)).length ?? 0;
  return (
    <div className="sessions-page monitor-page">
      <div className="page-head">
        <h2>
          <Activity size={20} /> SQL Monitor
        </h2>
        <p className="page-sub">
          This server’s recent queries and every active statement, including results still being fetched.
        </p>
        <button type="button" className="btn btn-ghost" onClick={() => setRefresh((n) => n + 1)} disabled={!connection}>
          <RefreshCw size={14} /> Refresh
        </button>
      </div>
      {(connectionError || error) && (
        <div className="result-error" role="alert">
          <pre>{connectionError ?? error}</pre>
          {updated && <p>Showing the last successful sample from {timestamp(updated)}.</p>}
        </div>
      )}
      {disabled ? (
        <div className="monitor-empty">
          <h3>Instrumentation is disabled</h3>
          <p>
            Start the server with <code>--enable-instrumentation</code> to enable SQL Monitor and record query history.
          </p>
        </div>
      ) : (
        <>
          {info && (
            <div className="monitor-stats" aria-label="Statement queue status">
              <div>
                <span>Statement queueing</span>
                <strong>{info.queueing_enabled ? "Enabled" : "Disabled · unlimited concurrency"}</strong>
              </div>
              <div>
                <span>Enforced slots in use / limit</span>
                <strong>
                  {info.queueing_enabled ? `${info.slots_in_use} / ${info.concurrency_limit}` : "Unlimited"}
                </strong>
              </div>
              <div>
                <span>Slots available</span>
                <strong>{info.slots_available?.toString() ?? "Unlimited"}</strong>
              </div>
              <div>
                <span>Queued statements</span>
                <strong>
                  {info.queued_statements.toString()} /{" "}
                  {info.queue_capacity > 0n ? info.queue_capacity.toString() : "unbounded"}
                </strong>
              </div>
            </div>
          )}
          {info && (
            <p className="page-sub">
              Administrators bypass the queue by default: <strong>{info.admin_bypass_default ? "Yes" : "No"}</strong>.
              This monitor uses a separate session that bypasses the queue. Bypassed statements do not consume enforced
              slots. Queue wait limit:{" "}
              {info.max_queue_wait_seconds > 0n ? `${info.max_queue_wait_seconds} seconds` : "unlimited"}.
            </p>
          )}
          <div className="monitor-controls">
            <label>
              Recent history{" "}
              <select aria-label="Recent history" value={minutes} onChange={(e) => setMinutes(Number(e.target.value))}>
                {[1, 5, 10, 30, 60, 360, 1440].map((m) => (
                  <option key={m} value={m}>
                    {m < 60 ? `${m} minutes` : `${m / 60} hours`}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Status{" "}
              <select aria-label="Query status" value={state} onChange={(e) => setState(e.target.value)}>
                {[
                  ["all", "All"],
                  ["active", "All active"],
                  ["queued", "Queued"],
                  ["executing", "Executing"],
                  ["fetching", "Fetching"],
                  ["success", "Succeeded"],
                  ["error", "Failed"],
                  ["timeout", "Timed out"],
                  ["cancelled", "Cancelled"],
                ].map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <input
              aria-label="Filter monitored queries"
              placeholder="Filter SQL, user, or execution ID"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
            <label>
              <input type="checkbox" checked={includeMonitor} onChange={(e) => setIncludeMonitor(e.target.checked)} />{" "}
              Include monitor queries
            </label>
            <button type="button" className="btn btn-ghost" onClick={() => setPaused((v) => !v)}>
              {paused ? <Play size={14} /> : <Pause size={14} />}
              {paused ? "Resume" : "Pause"}
            </button>
          </div>
          <p className="page-sub" role="status">
            {updated
              ? `${rows.length} statements · ${paused ? "Paused" : "Refreshes every 2 seconds"} · Updated ${timestamp(updated)}`
              : "Loading…"}{" "}
            · Active statements always remain in the time window; status and text filters still apply.
          </p>
          {info && (
            <p className={`page-sub${info.history_dropped > 0n || info.history_failed > 0n ? " monitor-warning" : ""}`}>
              History is asynchronous · {info.history_backlog.toString()} events pending ·{" "}
              {info.history_dropped.toString()} dropped · {info.history_failed.toString()} failed.{" "}
              {info.history_dropped > 0n || info.history_failed > 0n
                ? "Recorded history may be incomplete; live statements are shown independently."
                : ""}
            </p>
          )}
          {terminalCount >= historyLimit && (
            <p className="results-notice">
              Showing up to {historyLimit.toLocaleString()} recorded completions plus recent live completions. Active
              statements are never capped.
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() => setHistoryLimit((n) => Math.min(1_000_000, n + 1000))}
                disabled={historyLimit >= 1_000_000}
              >
                Load 1,000 more completions
              </button>
            </p>
          )}
          <div className="monitor-table-wrap">
            <table className="data-table monitor-table" data-testid="sql-monitor-table">
              <thead>
                <tr>
                  <th>Status</th>
                  <th>User</th>
                  <th>SQL / Execution</th>
                  <th>Queued / Started</th>
                  <th>Queue wait</th>
                  <th>Elapsed</th>
                  <th>Rows fetched</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((q) => (
                  <tr key={q.execution_id} className={q.execution_id === selected ? "row-current" : undefined}>
                    <td>
                      <span className={`monitor-status monitor-status-${q.status}`}>{statusLabel(q)}</span>
                    </td>
                    <td>{q.username}</td>
                    <td>
                      <button
                        type="button"
                        className="monitor-sql mono"
                        onClick={() => setSelected(q.execution_id)}
                        title="View statement details"
                      >
                        {q.sql_text || "(SQL unavailable)"}
                      </button>
                      <small className="mono">{q.execution_id}</small>
                    </td>
                    <td>{timestamp(q.enqueue_time ?? q.execution_start_time)}</td>
                    <td>
                      {q.enqueue_time == null
                        ? "—"
                        : duration(
                            (q.status === "queued"
                              ? now
                              : q.cancel_reason === "queue_timeout"
                                ? (q.cursor_close_time ?? q.execution_end_time ?? now)
                                : q.execution_start_time) - q.enqueue_time,
                          )}
                    </td>
                    <td>
                      {duration(
                        (q.cursor_close_time ?? q.fetch_end_time ?? now) - (q.enqueue_time ?? q.execution_start_time),
                      )}
                    </td>
                    <td>{q.rows_fetched?.toLocaleString() ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {queries && rows.length === 0 && <p className="monitor-empty">No statements match this window and filter.</p>}
          {inspected && (
            <section className="monitor-detail" aria-label="Statement details">
              <div className="page-head">
                <h3>{statusLabel(inspected)}</h3>
                <button type="button" className="btn btn-ghost" onClick={() => setSelected(undefined)}>
                  Close details
                </button>
              </div>
              <pre className="value-text">{inspected.sql_text}</pre>
              <dl>
                <dt>Execution / statement</dt>
                <dd className="mono">
                  {inspected.execution_id} / {inspected.statement_id}
                </dd>
                <dt>Session / user</dt>
                <dd>
                  {inspected.session_id} / {inspected.username}
                </dd>
                <dt>Enqueued</dt>
                <dd>{timestamp(inspected.enqueue_time)}</dd>
                <dt>Execution started / produced results</dt>
                <dd>
                  {timestamp(inspected.execution_start_time)} / {timestamp(inspected.execution_end_time)}
                </dd>
                <dt>Fetch started / ended</dt>
                <dd>
                  {timestamp(inspected.fetch_start_time)} / {timestamp(inspected.fetch_end_time)}
                </dd>
                <dt>Cursor closed</dt>
                <dd>{timestamp(inspected.cursor_close_time)}</dd>
                <dt>Cancellation reason</dt>
                <dd>{inspected.cancel_reason || "—"}</dd>
              </dl>
              {inspected.error_message && <pre className="result-error">{inspected.error_message}</pre>}
            </section>
          )}
          <p className="page-sub">
            Fetching means the server is reading or retaining the result for the client; these timestamps measure server
            processing, not confirmed delivery to the client. SQL uses the server’s redacted statement text.
          </p>
        </>
      )}
    </div>
  );
}
