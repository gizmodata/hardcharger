import type { Connection } from "@gizmodata/chargewire";
import { KeyRound, LogOut } from "lucide-react";
import { type SyntheticEvent, useState } from "react";

import { type SignedIn, SignInError, signIn } from "../lib/auth";
import { changeOwnPasswordSql } from "../lib/passwordChange";
import { internalQueryTag } from "../lib/queryTags";
import { errorMessage } from "../session";
import { Wordmark } from "./Brand";

interface Props {
  /** The sign-in whose password is temporary. */
  auth: SignedIn;
  /** Its session: the server runs nothing on it but the change. */
  connection: Connection;
  /** Signed in again with the new password. */
  onChanged: (auth: SignedIn) => void;
  onSignOut: () => void;
}

/**
 * Shown when the server holds the session until its password is changed: the password was
 * set by someone else (a help desk's reset, or an administrator's EXPIRE). The user gives the
 * current (temporary) password and a new one; the change ends the session, and the page signs
 * in again with the new password.
 */
export function ChangePasswordPage({ auth, connection, onChanged, onSignOut }: Props) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState<string | undefined>();
  const [working, setWorking] = useState(false);

  const submit = (e: SyntheticEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (working) return;
    if (next !== again) {
      setError("The new passwords do not match.");
      return;
    }
    if (next === current) {
      setError("Choose a new password, not the one you were given.");
      return;
    }
    setWorking(true);
    setError(undefined);
    void (async () => {
      try {
        await connection.query(changeOwnPasswordSql(next, current), undefined, { queryTag: internalQueryTag });
      } catch (err) {
        setError(errorMessage(err));
        setWorking(false);
        return;
      }
      // The change ended this session (its token was issued for the old password).
      void connection.close().catch(() => {});
      try {
        const renewed = await signIn(auth.username, next);
        setCurrent("");
        setNext("");
        setAgain("");
        onChanged(renewed);
      } catch (err) {
        setError(
          `Your password was changed, but signing in again failed: ${err instanceof SignInError ? err.message : String(err)}`,
        );
        setWorking(false);
      }
    })();
  };

  return (
    <div className="signin">
      <form className="signin-card" onSubmit={submit} aria-label="Change your password">
        <h1 className="signin-brand">
          <Wordmark size="signin" />
        </h1>
        <p className="signin-notice" role="status" data-testid="password-must-change">
          The password of {auth.username} was set by someone else. Choose your own before you go on.
        </p>
        <label className="field">
          <span>Current password</span>
          <input
            name="current-password"
            type="password"
            autoComplete="current-password"
            autoFocus
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            required
            disabled={working}
          />
        </label>
        <label className="field">
          <span>New password</span>
          <input
            name="new-password"
            type="password"
            autoComplete="new-password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            required
            disabled={working}
          />
        </label>
        <label className="field">
          <span>New password again</span>
          <input
            name="new-password-again"
            type="password"
            autoComplete="new-password"
            value={again}
            onChange={(e) => setAgain(e.target.value)}
            required
            disabled={working}
          />
        </label>
        {error && (
          <p className="signin-error" role="alert" data-testid="password-change-error">
            {error}
          </p>
        )}
        <button
          type="submit"
          className="btn btn-primary signin-submit"
          disabled={working || !current || !next || !again}
        >
          <KeyRound size={16} /> {working ? "Changing…" : "Change password"}
        </button>
        <button type="button" className="btn btn-secondary signin-submit" onClick={onSignOut} disabled={working}>
          <LogOut size={16} /> Sign out
        </button>
      </form>
    </div>
  );
}
