import { RefreshCw, Settings } from "lucide-react";
import { useEffect, useState } from "react";

import { useAdminConnection } from "../lib/adminConnection";
import { errorMessage, useSession } from "../session";

interface Setting {
  name: string;
  value: string;
  global_value: string | null;
  scope: string;
  settable: boolean;
  cli_flag: string | null;
  default_value: string;
  description: string;
}

export default function ServerConfigurationPage() {
  const session = useSession();
  const { connection, allowed, error: connectionError } = useAdminConnection("server-configuration");
  const [settings, setSettings] = useState<Setting[]>();
  const [facts, setFacts] = useState<Record<string, unknown>>();
  const [error, setError] = useState<string>();
  const [filter, setFilter] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(true);

  useEffect(() => {
    if (!connection) return;
    let live = true;
    const load = async () => {
      try {
        const table = await connection.query("SELECT * FROM hardcharger_server_settings() ORDER BY name");
        if (!live) return;
        const info = await connection.query("SELECT * FROM hardcharger_server_info()");
        if (!live) return;
        setSettings([...table].map((r) => (r as unknown as { toJSON: () => Setting }).toJSON()));
        setFacts(info.get(0)?.toJSON());
        setError(undefined);
      } catch (err) {
        if (live && !session.checkSignIn(err)) setError(errorMessage(err));
      } finally {
        if (live) setBusy(false);
      }
    };
    void load();
    return () => {
      live = false;
    };
  }, [connection, session, refresh]);

  if (!allowed)
    return (
      <div className="sessions-page">
        <h2>Server Configuration</h2>
        <p>Only unrestricted administrators can view server configuration.</p>
      </div>
    );
  const shown = settings?.filter((s) =>
    `${s.name} ${s.description} ${s.cli_flag}`.toLowerCase().includes(filter.toLowerCase()),
  );
  return (
    <div className="sessions-page configuration-page">
      <div className="page-head">
        <h2>
          <Settings size={20} /> Server Configuration
        </h2>
        <p className="page-sub">Current server settings and startup facts, available to unrestricted administrators.</p>
        <button
          type="button"
          className="btn btn-ghost"
          disabled={busy || !connection}
          onClick={() => {
            setBusy(true);
            setRefresh((n) => n + 1);
          }}
        >
          <RefreshCw size={14} /> Refresh
        </button>
      </div>
      {(error || connectionError) && (
        <div className="result-error" role="alert">
          <pre>{error ?? connectionError}</pre>
        </div>
      )}
      <p className="page-sub">
        This view is read-only. Runtime settings can be changed with SQL by an authorized administrator; startup
        settings require a server restart. Credentials, private keys, and connection strings are not exposed. Settings
        are the server’s curated Hardcharger settings; they are not a full YAML configuration export.
      </p>
      {facts && (
        <details className="monitor-detail">
          <summary>Server identity, listeners, and security</summary>
          <dl className="configuration-facts">
            {Object.entries(facts)
              .filter(([name]) => !["uptime", "start_time"].includes(name))
              .map(([name, value]) => (
                <div key={name}>
                  <dt className="mono">{name}</dt>
                  <dd>
                    {value == null
                      ? "—"
                      : typeof value === "string" ||
                          typeof value === "number" ||
                          typeof value === "boolean" ||
                          typeof value === "bigint"
                        ? String(value)
                        : JSON.stringify(value)}
                  </dd>
                </div>
              ))}
          </dl>
        </details>
      )}
      <div className="monitor-controls">
        <input
          aria-label="Filter server settings"
          placeholder="Filter settings"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
      </div>
      <div className="monitor-table-wrap">
        <table className="data-table" data-testid="server-configuration-table">
          <thead>
            <tr>
              <th>Setting</th>
              <th>Current server value</th>
              <th>Applies at</th>
              <th>Default</th>
              <th>Startup flag</th>
              <th>Description</th>
            </tr>
          </thead>
          <tbody>
            {shown?.map((s) => (
              <tr key={s.name}>
                <td className="mono">{s.name}</td>
                <td className="mono">{s.global_value ?? s.value}</td>
                <td>
                  {s.settable ? (s.scope === "session" ? "Session" : "Runtime") : "Startup · restart required"}
                  {s.scope === "session" && <small>Observer session value</small>}
                </td>
                <td className="mono">{s.default_value}</td>
                <td className="mono">{s.cli_flag ?? "—"}</td>
                <td>{s.description}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {settings && shown?.length === 0 && <p>No settings match the filter.</p>}
      {!settings && !error && !connectionError && <p className="loading-line">Loading server configuration…</p>}
    </div>
  );
}
