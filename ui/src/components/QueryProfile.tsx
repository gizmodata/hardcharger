import { Download, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { useAdminConnection } from "../lib/adminConnection";
import { saveFile } from "../lib/download";
import { prettyJson } from "../lib/prettyJson";
import { parseProfile, profileMetricText, profileSql, type ProfileMetric } from "../lib/queryProfile";
import { errorMessage, useSession } from "../session";

import { ProfilePlan } from "./ProfilePlan";

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
export function QueryProfile({ executionId, server = "" }: { executionId: string; server?: string }) {
  const session = useSession();
  const { connection, error: connectionError } = useAdminConnection("sql-monitor", server);
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
            if (live && (server || !session.checkSignIn(err))) setError(errorMessage(err));
          }
        };
        await poll();
      } catch (err) {
        if (live && (server || !session.checkSignIn(err))) setError(errorMessage(err));
      }
    };
    void load();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [connection, session, executionId, refresh, server]);
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
          <ProfilePlan key={executionId} profile={parsed.profile} />
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
