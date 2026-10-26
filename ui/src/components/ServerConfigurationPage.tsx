import { RefreshCw, Settings } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { useAdminConnection } from "../lib/adminConnection";
import { useActiveTab } from "../lib/activeTab";
import { quoteLiteral } from "../lib/sql";
import {
  changeSettingSql,
  editableAt,
  sessionSettingValue,
  settingChoices,
  type ServerSetting,
  type SettingScope,
} from "../lib/serverSettings";
import { errorMessage, useSession } from "../session";

interface Edit {
  setting: ServerSetting;
  scope: SettingScope;
  sessionId: string;
  value: string;
}

export default function ServerConfigurationPage() {
  const session = useSession();
  const active = useActiveTab();
  const { connection, allowed, error: connectionError } = useAdminConnection("server-configuration");
  const [settings, setSettings] = useState<ServerSetting[]>();
  const [target, setTarget] = useState<{ id: string; settings: ServerSetting[] }>();
  const [sessionChoice, setSessionChoice] = useState("editor");
  const [facts, setFacts] = useState<Record<string, unknown>>();
  const [error, setError] = useState<string>();
  const [sessionError, setSessionError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [filter, setFilter] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(true);
  const [edit, setEdit] = useState<Edit>();
  const sequence = useRef(Promise.resolve());
  const editForm = useRef<HTMLFormElement>(null);
  const editKey = edit ? `${edit.scope}:${edit.setting.name}:${edit.sessionId}` : "";
  useEffect(() => {
    if (!editKey) return;
    editForm.current?.scrollIntoView({ block: "nearest" });
    editForm.current?.querySelector<HTMLElement>("input, select")?.focus();
  }, [editKey]);
  const editorSelected = sessionChoice === "editor" && Boolean(active.sessionId);
  const targetId = editorSelected ? active.sessionId! : session.identity.sessionId;

  useEffect(() => {
    if (!connection) return;
    let live = true;
    const load = async () => {
      if (!live) return;
      try {
        const table = await connection.query("SELECT * FROM hardcharger_server_settings() ORDER BY name");
        if (!live) return;
        setSettings([...table].map((r) => (r as unknown as { toJSON: () => ServerSetting }).toJSON()));
        const info = await connection.query("SELECT * FROM hardcharger_server_info()");
        if (!live) return;
        setFacts(info.get(0)?.toJSON());
        setError(undefined);
        try {
          const selected = await connection.query(
            `SELECT * FROM hardcharger_session_settings(${quoteLiteral(targetId)}) ORDER BY name`,
          );
          if (!live) return;
          setTarget({
            id: targetId,
            settings: [...selected].map((r) => (r as unknown as { toJSON: () => ServerSetting }).toJSON()),
          });
          setSessionError(undefined);
        } catch (err) {
          if (live && !session.checkSignIn(err)) {
            setTarget(undefined);
            setSessionError(errorMessage(err));
          }
        }
      } catch (err) {
        if (live && !session.checkSignIn(err)) setError(errorMessage(err));
      } finally {
        if (live) setBusy(false);
      }
    };
    sequence.current = sequence.current.then(load);
    return () => {
      live = false;
    };
  }, [connection, session, targetId, refresh]);

  const save = async (reset: boolean) => {
    if (!connection || !edit || busy) return;
    const change = edit;
    setBusy(true);
    setError(undefined);
    setNotice(undefined);
    const operation = sequence.current.then(async () => {
      await connection.execute(changeSettingSql(change.setting, change.scope, change.sessionId, change.value, reset));
    });
    sequence.current = operation.catch(() => {});
    try {
      await operation;
      setNotice(
        `${change.setting.name} ${reset ? "reset" : "saved"} ${change.scope === "GLOBAL" ? "globally" : `for session ${change.sessionId}`}.`,
      );
      setEdit(undefined);
      setRefresh((n) => n + 1);
    } catch (err) {
      if (!session.checkSignIn(err)) setError(errorMessage(err));
      setBusy(false);
    }
  };

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
  const selectedSettings = target?.id === targetId ? target.settings : undefined;
  const selectedByName = new Map(selectedSettings?.map((s) => [s.name, s]));
  const beginEdit = (setting: ServerSetting, scope: SettingScope) => {
    const current = selectedByName.get(setting.name);
    setError(undefined);
    setNotice(undefined);
    setEdit({
      setting,
      scope,
      sessionId: targetId,
      value:
        scope === "GLOBAL"
          ? (setting.global_value ?? setting.value)
          : current
            ? sessionSettingValue(current, selectedSettings ?? [])
            : "",
    });
  };
  const choices = edit ? settingChoices(edit.setting) : undefined;
  return (
    <div className="sessions-page configuration-page">
      <div className="page-head">
        <h2>
          <Settings size={20} /> Server Configuration
        </h2>
        <p className="page-sub">
          Session values, global values, and settings that can be changed while the server runs.
        </p>
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
      {notice && (
        <p className="results-notice" role="status">
          {notice}
        </p>
      )}
      <p className="page-sub">
        Session changes affect only the selected session. Global changes apply to sessions using that global value;
        explicit session overrides remain in force. Runtime changes are not saved to startup YAML and are lost on
        restart. Startup settings require a restart.
      </p>
      <div className="monitor-controls">
        <label>
          Current session{" "}
          <select
            aria-label="Settings session"
            value={editorSelected ? "editor" : "workspace"}
            disabled={busy || Boolean(edit)}
            onChange={(e) => {
              setBusy(true);
              setSessionChoice(e.target.value);
            }}
          >
            {active.sessionId && <option value="editor">Active editor — {active.title || "Query"}</option>}
            <option value="workspace">Workspace session</option>
          </select>
        </label>
        <span className="mono" data-testid="settings-session-id">
          {targetId}
        </span>
      </div>
      {!active.sessionId && (
        <p className="page-sub">
          Each SQL editor tab has its own session. Run a query in an editor tab to select and configure that session
          here.
        </p>
      )}
      {sessionError && (
        <p className="result-error" role="alert">
          Cannot read the selected session: {sessionError}. Global settings remain available.
        </p>
      )}
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
      {edit && (
        <form
          ref={editForm}
          className="monitor-detail setting-editor"
          aria-label={`Edit ${edit.setting.name}`}
          onSubmit={(e) => {
            e.preventDefault();
            void save(false);
          }}
        >
          <h3>
            {edit.setting.name} · {edit.scope === "GLOBAL" ? "Global" : "Session"}
          </h3>
          <p>{edit.setting.description}</p>
          <p className="page-sub">
            {edit.scope === "GLOBAL"
              ? "Changes the server-wide value for sessions without an override. Reset restores this server’s startup value."
              : `Changes only session ${edit.sessionId}. Reset removes its override and inherits the global value or session default.`}
          </p>
          <label>
            New value{" "}
            {choices ? (
              <select
                aria-label="New setting value"
                value={edit.value}
                disabled={busy}
                onChange={(e) => setEdit({ ...edit, value: e.target.value })}
              >
                {choices.map((v) => (
                  <option key={v} value={v}>
                    {v}
                  </option>
                ))}
              </select>
            ) : (
              <input
                aria-label="New setting value"
                value={edit.value}
                disabled={busy}
                inputMode={edit.setting.input_type === "INTEGER" ? "numeric" : "text"}
                onChange={(e) => setEdit({ ...edit, value: e.target.value })}
              />
            )}
          </label>
          <div className="monitor-controls">
            <button type="submit" className="btn btn-primary" disabled={busy}>
              {busy ? "Saving…" : "Apply change"}
            </button>
            <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => void save(true)}>
              {edit.scope === "GLOBAL" ? "Reset to startup value" : "Reset session override"}
            </button>
            <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => setEdit(undefined)}>
              Cancel
            </button>
          </div>
        </form>
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
        <table className="data-table configuration-table" data-testid="server-configuration-table">
          <thead>
            <tr>
              <th>Setting</th>
              <th>Current session value</th>
              <th>Global value</th>
              <th>Scope</th>
              <th>Description</th>
            </tr>
          </thead>
          <tbody>
            {shown?.map((setting) => {
              const current = selectedByName.get(setting.name);
              const hasSession = setting.scope === "SESSION" || setting.scope === "SESSION_OR_GLOBAL";
              return (
                <tr key={setting.name} data-setting={setting.name}>
                  <td className="mono">
                    {setting.name}
                    <small>Default: {setting.default_value || "(empty)"}</small>
                    {setting.cli_flag && <small>{setting.cli_flag}</small>}
                  </td>
                  <td className="mono" data-testid="setting-session-value">
                    {hasSession
                      ? current
                        ? sessionSettingValue(current, selectedSettings ?? []) || "(empty)"
                        : "—"
                      : "—"}
                    {hasSession && current && (
                      <small>{current.session_value === null ? "Inherited" : "Session override"}</small>
                    )}
                    {editableAt(setting, "SESSION") && (
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        disabled={busy || !current || Boolean(edit)}
                        onClick={() => beginEdit(setting, "SESSION")}
                        aria-label={`Edit session ${setting.name}`}
                      >
                        Edit session
                      </button>
                    )}
                  </td>
                  <td className="mono" data-testid="setting-global-value">
                    {setting.scope === "SESSION" ? "—" : (setting.global_value ?? setting.value)}
                    {editableAt(setting, "GLOBAL") && (
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        disabled={busy || Boolean(edit)}
                        onClick={() => beginEdit(setting, "GLOBAL")}
                        aria-label={`Edit global ${setting.name}`}
                      >
                        Edit global
                      </button>
                    )}
                  </td>
                  <td>
                    {setting.scope === "STARTUP"
                      ? "Startup · restart required"
                      : setting.scope === "SESSION_OR_GLOBAL"
                        ? "Session or global"
                        : setting.scope === "SESSION"
                          ? "Session only"
                          : "Global only"}
                  </td>
                  <td>{setting.description}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="page-sub">
        Credentials, private keys, and connection strings are not exposed. Only settings the server marks as runtime
        adjustable have editing controls.
      </p>
      {settings && shown?.length === 0 && <p>No settings match the filter.</p>}
      {!settings && !error && !connectionError && <p className="loading-line">Loading server configuration…</p>}
    </div>
  );
}
