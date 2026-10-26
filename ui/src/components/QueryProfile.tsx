import { Download, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { useAdminConnection } from "../lib/adminConnection";
import { saveFile } from "../lib/download";
import { prettyJson } from "../lib/prettyJson";
import { parseProfile, profileMetricText, profileSql, type ProfileMetric } from "../lib/queryProfile";
import { errorMessage, useSession } from "../session";

function Metrics({ metrics }: { metrics: ProfileMetric[] }) {
  return (
    <dl className="profile-metrics">
      {metrics.map((metric) => (
        <div key={metric.name}>
          <dt className="mono">{metric.name}</dt>
          <dd>{profileMetricText(metric.value, metric.unit)}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Fetch only the selected execution's stored profile. No EXPLAIN or replay. */
export function QueryProfile({ executionId }: { executionId: string }) {
  const session = useSession();
  const { connection, error: connectionError } = useAdminConnection("sql-monitor");
  const [raw, setRaw] = useState<string>();
  const [units, setUnits] = useState<ReadonlyMap<string, string>>(new Map());
  const [mode, setMode] = useState<string>();
  const [state, setState] = useState("loading");
  const [error, setError] = useState<string>();
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    if (!connection) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const location = await connection.query(
          "SELECT HARDCHARGER_INSTRUMENTATION_CATALOG() AS catalog, HARDCHARGER_INSTRUMENTATION_SCHEMA() AS schema",
        );
        if (!live) return;
        const at = location.get(0)!.toJSON() as { catalog: string; schema: string };
        const settings = await connection.query(
          "SELECT global_value FROM hardcharger_server_settings() WHERE name = 'hardcharger.capture_query_profile'",
        );
        if (!live) return;
        setMode(settings.getChildAt(0)?.get(0) as string | undefined);
        const definitions = await connection.query("SELECT metric_name, unit FROM duckdb_available_metrics()");
        if (!live) return;
        setUnits(
          new Map(
            [...definitions].map((r: unknown) => {
              const row = (r as { toJSON: () => { metric_name: string; unit: string | null } }).toJSON();
              return [row.metric_name, row.unit ?? ""];
            }),
          ),
        );
        const poll = async () => {
          try {
            const result = await connection.query(profileSql(at.catalog, at.schema, executionId));
            if (!live) return;
            const row = result.get(0)?.toJSON() as { profile: string | null; finished: boolean } | undefined;
            setError(undefined);
            if (row?.profile) {
              setRaw(row.profile);
              setState("ready");
            } else if (row?.finished) setState("unavailable");
            else {
              setState("pending");
              timer = setTimeout(() => void poll(), 2000);
            }
          } catch (err) {
            if (live && !session.checkSignIn(err)) setError(errorMessage(err));
          }
        };
        await poll();
      } catch (err) {
        if (live && !session.checkSignIn(err)) setError(errorMessage(err));
      }
    };
    void load();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [connection, session, executionId, refresh]);
  const parsed = useMemo(() => {
    if (!raw) return undefined;
    try {
      return { profile: parseProfile(raw, units) };
    } catch {
      return {
        error:
          "This captured profile cannot be displayed as a DuckDB 2.0 metric tree. The original JSON is available below.",
      };
    }
  }, [raw, units]);
  const summaryNames = [
    "query.total_time",
    "query.cpu_time",
    "system.peak_buffer_memory",
    "system.peak_temp_dir_size",
    "io.total_bytes_read",
    "io.total_bytes_written",
  ];
  const summary = parsed?.profile?.groups
    .flatMap((g) => g.metrics)
    .filter((m) => summaryNames.includes(m.name))
    .sort((a, b) => summaryNames.indexOf(a.name) - summaryNames.indexOf(b.name));
  return (
    <section className="query-profile" aria-label="Captured query profile">
      <div className="page-head">
        <h3>Captured query profile</h3>
        {raw && (
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => saveFile(new Blob([raw], { type: "application/json" }), `query-profile-${executionId}.json`)}
          >
            <Download size={14} /> Download profile JSON
          </button>
        )}
      </div>
      <p className="page-sub">
        Stored instrumentation for this execution. Current global capture: <strong>{mode ?? "loading…"}</strong>{" "}
        (session overrides may differ).
      </p>
      {(error || connectionError) && (
        <div role="alert" className="result-error">
          <pre>{error ?? connectionError}</pre>
        </div>
      )}
      {!raw && (
        <p role="status">
          {state === "unavailable"
            ? "No profile was captured for this execution. Capture may have been off, or the query did not produce a profile. Enabling profiling does not create profiles for past executions."
            : state === "pending"
              ? "Waiting for the stored profile. It becomes available after the result closes and instrumentation writes its history; missing history may also prevent a profile from appearing."
              : "Loading the stored profile…"}
        </p>
      )}
      {(error || connectionError || state === "unavailable") && (
        <button type="button" className="btn btn-ghost" onClick={() => setRefresh((n) => n + 1)}>
          <RefreshCw size={14} /> Retry profile
        </button>
      )}
      {summary && summary.length > 0 && (
        <div className="profile-summary">
          <Metrics metrics={summary} />
        </div>
      )}
      {parsed?.profile && (
        <>
          <h4>Execution plan</h4>
          <p className="page-sub">
            Operator timing and CPU measurements can overlap across parallel work; they are not percentages of total
            elapsed time. Missing metrics are shown as —.
          </p>
          {parsed.profile.operators.length > 0 ? (
            <div className="monitor-table-wrap">
              <table className="data-table profile-plan" data-testid="query-profile-plan">
                <thead>
                  <tr>
                    <th>Operator</th>
                    <th>Time</th>
                    <th>Actual rows</th>
                    <th>Estimated rows</th>
                    <th>Rows scanned</th>
                    <th>Row groups scanned / total</th>
                    <th>Details</th>
                  </tr>
                </thead>
                <tbody>
                  {parsed.profile.operators.map((op, i) => (
                    <tr key={i}>
                      <td className="mono" style={{ paddingLeft: 10 + Math.min(op.depth, 20) * 16 }}>
                        {op.depth > 0 ? "↳ " : ""}
                        {op.name}
                      </td>
                      <td>{op.timing === undefined ? "—" : profileMetricText(op.timing, "seconds")}</td>
                      <td>{op.rows?.toLocaleString() ?? "—"}</td>
                      <td>{op.estimatedRows ?? "—"}</td>
                      <td>{op.scanned?.toLocaleString() ?? "—"}</td>
                      <td>
                        {op.rowGroups === undefined ? "—" : op.rowGroups.toLocaleString()} /{" "}
                        {op.totalRowGroups?.toLocaleString() ?? "—"}
                      </td>
                      <td>
                        <details>
                          <summary>Operator details</summary>
                          <Metrics metrics={op.metrics} />
                          <pre className="value-text">{JSON.stringify(op.extra, null, 2)}</pre>
                        </details>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p>No operator metrics were captured.</p>
          )}
          {parsed.profile.operatorsTruncated && (
            <p>Showing the first 5,000 operators. Download the full profile to inspect the remainder.</p>
          )}
          <h4>Captured metrics</h4>
          {parsed.profile.groups.map((group) => (
            <details className="profile-group" key={group.name} open={["query", "system", "io"].includes(group.name)}>
              <summary>{group.name.replaceAll("_", " ")}</summary>
              <Metrics metrics={group.metrics} />
            </details>
          ))}
        </>
      )}
      {parsed?.error && <p className="results-notice">{parsed.error}</p>}
      {raw && (
        <details className="profile-group">
          <summary>Raw captured profile JSON</summary>
          <pre className="value-text">{prettyJson(raw)}</pre>
        </details>
      )}
      <p className="page-sub">
        Metrics and units:{" "}
        <a href="https://duckdb.org/docs/2.0/dev/metrics" target="_blank" rel="noreferrer">
          DuckDB 2.0 metrics reference
        </a>
        . Only metrics present in the captured profile are displayed.
      </p>
    </section>
  );
}
