import { Moon, RefreshCw, Sun, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { useAdminConnection } from "../lib/adminConnection";
import { queryDetailSql, type MonitoredQuery } from "../lib/sqlMonitor";
import type { Theme } from "../lib/theme";
import { errorMessage, useSession } from "../session";
import { Wordmark } from "./Brand";
import { StatementDetails } from "./StatementDetails";

/** An independent observer: its execution remains selected when the monitor's
 * filters change, its history window moves on, or the original workspace closes. */
export default function QueryDetailPage({ theme }: { theme: Theme }) {
  const session = useSession();
  const { connection, error: connectionError, allowed } = useAdminConnection("sql-monitor");
  const executionId = new URLSearchParams(window.location.search).get("execution") ?? "";
  const [query, setQuery] = useState<MonitoredQuery>();
  const [error, setError] = useState<string>();
  const [updated, setUpdated] = useState<number>();
  const [missing, setMissing] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const sampling = useRef(Promise.resolve());
  useEffect(() => {
    document.title = `Hardcharger — Query ${executionId || "details"}`;
    // The browser has already copied sessionStorage from the opener. The window
    // now owns its sign-in and sessions, with no ongoing dependency on the opener.
    window.opener = null;
  }, [executionId]);
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
        const result = await connection.query(queryDetailSql(at.catalog, at.schema, executionId));
        if (!live) return;
        const row = result.get(0)?.toJSON() as unknown as MonitoredQuery | undefined;
        if (row) setQuery(row);
        setMissing(!row);
        setError(undefined);
        setUpdated(Date.now());
      } catch (err) {
        if (live && !session.checkSignIn(err)) setError(errorMessage(err));
      } finally {
        if (live) timer = setTimeout(sample, 2000);
      }
    };
    const sample = () => {
      sampling.current = sampling.current.then(async () => {
        if (live) await load();
      });
    };
    sample();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [connection, session, executionId, refresh]);
  return (
    <div className="query-detail-window">
      <header className="app-header">
        <div className="header-left">
          <Wordmark size="header" />
          <h1>Query details</h1>
        </div>
        <div className="header-right">
          <span className="chip">{session.identity.user}</span>
          <button type="button" className="btn btn-ghost" onClick={theme.toggle} aria-label="Toggle color theme">
            {theme.name === "dark" ? <Sun size={16} /> : <Moon size={16} />}
          </button>
          <button type="button" className="btn btn-ghost" onClick={() => window.close()}>
            <X size={14} /> Close window
          </button>
        </div>
      </header>
      <main className="query-detail-content">
        {!allowed ? (
          <p>Only unrestricted administrators can view query details.</p>
        ) : session.identity.instrumentationEnabled === false ? (
          <p>Instrumentation is disabled on this server.</p>
        ) : (
          <>
            <div className="page-head">
              <p className="page-sub">
                {updated
                  ? `Refreshes every 2 seconds · Updated ${new Date(updated).toLocaleTimeString()}`
                  : "Loading query details…"}
              </p>
              <button
                type="button"
                className="btn btn-ghost"
                disabled={!connection}
                onClick={() => setRefresh((n) => n + 1)}
              >
                <RefreshCw size={14} /> Refresh
              </button>
            </div>
            {(error || connectionError) && (
              <div className="result-error" role="alert">
                <pre>{error ?? connectionError}</pre>
                {query && <p>Showing the last successful sample.</p>}
              </div>
            )}
            {missing && (
              <p role="status">
                This execution is not currently in live or recorded history.{" "}
                {query
                  ? "Showing the last successful sample."
                  : "Waiting for instrumentation; the history may have been removed or belong to another instance."}
              </p>
            )}
            {query && <StatementDetails query={query} />}
          </>
        )}
      </main>
    </div>
  );
}
