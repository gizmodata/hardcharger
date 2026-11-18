import { RefreshCw, XCircle } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import type { Connection } from "@gizmodata/chargewire";

import { useAdminConnection } from "../lib/adminConnection";
import { formatDate } from "../lib/format";
import { quoteLiteral } from "../lib/sql";
import { errorMessage, useSession } from "../session";
import { ConfirmSessionDialog } from "./ConfirmSessionDialog";
import { SystemAdminNotice } from "./SystemAdminNotice";

interface SessionRow {
  session_id: string;
  username: string;
  role: string;
  auth_method: string;
  start_time: number;
  running: boolean;
  is_current: boolean;
  client_ip: string | null;
  client_is_local: boolean | null;
}

function when(ms: number): string {
  const d = new Date(ms);
  return `${formatDate(ms)} ${d.toISOString().slice(11, 19)} UTC`;
}

async function readSessions(connection: Connection): Promise<SessionRow[]> {
  const table = await connection.query(
    "SELECT session_id, username, role, auth_method, start_time, running, is_current, client_ip, client_is_local " +
      "FROM hardcharger_sessions() ORDER BY start_time",
  );
  return [...table].map((r) => (r as unknown as { toJSON: () => SessionRow }).toJSON());
}

/**
 * The server's sessions (hardcharger_sessions()): an administrator sees every session and
 * can end one (KILL SESSION); everyone else sees only their own. A user one of whose tabs acts
 * as System Admin sees and ends them as the administrator does, through a session of this
 * page's own that acts as System Admin too (useAdminConnection), and only while it does.
 */
export default function SessionsPage() {
  const session = useSession();
  const observer = useAdminConnection("sessions", "", true);
  const systemAdmin = observer.systemAdmin;
  // The administrator reads through the UI's own session; a System Admin through the page's
  // elevated one, once it is open (until then nothing is read).
  const connection: Connection | undefined = systemAdmin ? observer.connection : session.connection;
  const administers = session.isAdmin || (systemAdmin && !!observer.connection);
  const [rows, setRows] = useState<SessionRow[] | undefined>();
  const [error, setError] = useState<string | undefined>();
  const [busy, setBusy] = useState(true);
  const [notice, setNotice] = useState<string | undefined>();
  const [confirmEnd, setConfirmEnd] = useState<SessionRow>();
  const [ending, setEnding] = useState<string>();

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
    if (!connection) return;
    let live = true;
    readSessions(connection).then(
      (rows) => live && apply({ rows }),
      (error: unknown) => live && apply({ error }),
    );
    return () => {
      live = false;
    };
  }, [connection, apply]);

  const load = async () => {
    if (!connection) return;
    setBusy(true);
    try {
      apply({ rows: await readSessions(connection) });
    } catch (error) {
      apply({ error });
    }
  };

  const kill = async (row: SessionRow) => {
    if (ending || !connection) return;
    setEnding(row.session_id);
    try {
      await connection.execute(`KILL SESSION ${quoteLiteral(row.session_id)}`);
      setNotice(`Ended session ${row.session_id}.`);
      await load();
    } catch (err) {
      if (!session.checkSignIn(err)) setNotice(errorMessage(err));
    } finally {
      setEnding(undefined);
    }
  };

  return (
    <div className="sessions-page">
      {confirmEnd && (
        <ConfirmSessionDialog
          sessionId={confirmEnd.session_id}
          username={confirmEnd.username}
          end
          onCancel={() => setConfirmEnd(undefined)}
          onConfirm={() => {
            setConfirmEnd(undefined);
            void kill(confirmEnd);
          }}
        />
      )}
      <div className="page-head">
        <h2>Sessions</h2>
        <p className="page-sub">
          {administers
            ? "Every session on this server. Ending one cancels its statements and signs its user out of it."
            : "Your sessions on this server (an administrator sees everyone's)."}
        </p>
        <button type="button" className="btn btn-ghost" onClick={() => void load()} disabled={busy}>
          <RefreshCw size={14} /> Refresh
        </button>
      </div>
      {systemAdmin && <SystemAdminNotice />}
      {notice && (
        <p className="results-notice" role="status">
          {notice}
        </p>
      )}
      {(error ?? (systemAdmin ? observer.error : undefined)) && (
        <div className="result-error" role="alert">
          <pre>{error ?? observer.error}</pre>
        </div>
      )}
      {rows && (
        <table className="data-table" data-testid="sessions-table">
          <thead>
            <tr>
              <th>Session</th>
              <th>User</th>
              <th>Client IP</th>
              <th>Role</th>
              <th>Sign-in</th>
              <th>Started</th>
              <th>State</th>
              {administers && <th aria-label="Actions" />}
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
                <td
                  className="mono"
                  title={
                    r.client_is_local ? `Connection from the computer running this server (${r.client_ip})` : undefined
                  }
                >
                  {r.client_is_local ? (
                    <>
                      This computer
                      <br />
                      <small>{r.client_ip}</small>
                    </>
                  ) : (
                    r.client_ip || "—"
                  )}
                </td>
                <td>{r.role}</td>
                <td>{r.auth_method}</td>
                <td>{when(r.start_time)}</td>
                <td>{r.running ? "running" : "idle"}</td>
                {administers && (
                  <td>
                    {!r.is_current && (
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm btn-danger-text"
                        onClick={() => setConfirmEnd(r)}
                        disabled={!!ending}
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
