import { RefreshCw, XCircle } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { formatDate } from "../lib/format";
import { quoteLiteral } from "../lib/sql";
import { type AppSession, errorMessage, useSession } from "../session";

interface SessionRow {
  session_id: string;
  username: string;
  role: string;
  auth_method: string;
  start_time: number;
  running: boolean;
  is_current: boolean;
}

function when(ms: number): string {
  const d = new Date(ms);
  return `${formatDate(ms)} ${d.toISOString().slice(11, 19)} UTC`;
}

async function readSessions(session: AppSession): Promise<SessionRow[]> {
  const table = await session.connection.query(
    "SELECT session_id, username, role, auth_method, start_time, running, is_current " +
      "FROM hardcharger_sessions() ORDER BY start_time",
  );
  return [...table].map((r) => (r as unknown as { toJSON: () => SessionRow }).toJSON());
}

/**
 * The server's sessions (hardcharger_sessions()): an administrator sees every session and
 * can end one (KILL SESSION); everyone else sees only their own.
 */
export default function SessionsPage() {
  const session = useSession();
  const [rows, setRows] = useState<SessionRow[] | undefined>();
  const [error, setError] = useState<string | undefined>();
  const [busy, setBusy] = useState(true);
  const [notice, setNotice] = useState<string | undefined>();

  // Applies an answer (or a failure); called from promise callbacks, after the query.
  const apply = useCallback(
    (result: { rows: SessionRow[] } | { error: unknown }) => {
      if ("rows" in result) {
        setRows(result.rows);
        setError(undefined);
      } else if (!session.checkSignIn(result.error)) {
        setError(errorMessage(result.error));
      }
      setBusy(false);
    },
    [session],
  );

  useEffect(() => {
    let live = true;
    readSessions(session).then(
      (rows) => live && apply({ rows }),
      (error: unknown) => live && apply({ error }),
    );
    return () => {
      live = false;
    };
  }, [session, apply]);

  const load = async () => {
    setBusy(true);
    try {
      apply({ rows: await readSessions(session) });
    } catch (error) {
      apply({ error });
    }
  };

  const kill = async (row: SessionRow) => {
    if (!window.confirm(`End ${row.username}'s session ${row.session_id}? Its running statements are cancelled.`)) {
      return;
    }
    try {
      await session.connection.execute(`KILL SESSION ${quoteLiteral(row.session_id)}`);
      setNotice(`Ended session ${row.session_id}.`);
      await load();
    } catch (err) {
      if (!session.checkSignIn(err)) setNotice(errorMessage(err));
    }
  };

  return (
    <div className="sessions-page">
      <div className="page-head">
        <h2>Sessions</h2>
        <p className="page-sub">
          {session.isAdmin
            ? "Every session on this server. Ending one cancels its statements and signs its user out of it."
            : "Your sessions on this server (an administrator sees everyone's)."}
        </p>
        <button type="button" className="btn btn-ghost" onClick={() => void load()} disabled={busy}>
          <RefreshCw size={14} /> Refresh
        </button>
      </div>
      {notice && (
        <p className="results-notice" role="status">
          {notice}
        </p>
      )}
      {error && (
        <div className="result-error" role="alert">
          <pre>{error}</pre>
        </div>
      )}
      {rows && (
        <table className="data-table" data-testid="sessions-table">
          <thead>
            <tr>
              <th>Session</th>
              <th>User</th>
              <th>Role</th>
              <th>Sign-in</th>
              <th>Started</th>
              <th>State</th>
              {session.isAdmin && <th aria-label="Actions" />}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.session_id} className={r.is_current ? "row-current" : undefined}>
                <td className="mono">
                  {r.session_id}
                  {r.is_current && <span className="tree-tag">this page</span>}
                </td>
                <td>{r.username}</td>
                <td>{r.role}</td>
                <td>{r.auth_method}</td>
                <td>{when(r.start_time)}</td>
                <td>{r.running ? "running" : "idle"}</td>
                {session.isAdmin && (
                  <td>
                    {!r.is_current && (
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm btn-danger-text"
                        onClick={() => void kill(r)}
                        aria-label={`End session ${r.session_id}`}
                      >
                        <XCircle size={14} /> End
                      </button>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
