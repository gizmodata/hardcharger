import type { DiscoveryDocument } from "@gizmodata/warp-sql";
import { LogIn, Moon, Sun } from "lucide-react";
import { type SyntheticEvent, useRef, useState } from "react";

import logo from "../assets/gizmodata-logo.png";
import { type SignedIn, SignInError, lastUsername, signIn } from "../lib/auth";
import type { Theme } from "../lib/theme";

interface Props {
  server: DiscoveryDocument | undefined;
  serverError: string | undefined;
  busy: boolean;
  notice: string | undefined;
  theme: Theme;
  onSignedIn: (auth: SignedIn) => void;
}

export function SignInPage({ server, serverError, busy, notice, theme, onSignedIn }: Props) {
  const [username, setUsername] = useState(lastUsername);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | undefined>();
  const [working, setWorking] = useState(false);
  const passwordRef = useRef<HTMLInputElement>(null);
  const passwordGrant = server?.auth?.grant_types?.includes("password") ?? true;

  const submit = async (e: SyntheticEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (working) return;
    setWorking(true);
    setError(undefined);
    try {
      const auth = await signIn(username.trim(), password);
      setPassword("");
      onSignedIn(auth);
    } catch (err) {
      setError(err instanceof SignInError ? err.message : String(err));
      setPassword("");
      passwordRef.current?.focus();
    } finally {
      setWorking(false);
    }
  };

  return (
    <div className="signin">
      <button
        type="button"
        className="btn btn-ghost btn-icon signin-theme"
        onClick={theme.toggle}
        title={theme.name === "light" ? "Dark theme" : "Light theme"}
        aria-label={theme.name === "light" ? "Dark theme" : "Light theme"}
      >
        {theme.name === "light" ? <Moon size={16} /> : <Sun size={16} />}
      </button>
      <form className="signin-card" onSubmit={(e) => void submit(e)} aria-label="Sign in">
        <div className="signin-brand">
          <img src={logo} alt="GizmoData" width={40} height={43} />
          <div>
            <h1>Hardcharger</h1>
            <p className="signin-server" data-testid="server-version">
              {server
                ? `${server.server?.name ?? "Warp SQL server"} ${server.server?.version ?? ""}`.trim()
                : serverError
                  ? "Server not reachable"
                  : "Connecting…"}
            </p>
          </div>
        </div>
        <div className="brand-rule" />
        {notice && (
          <p className="signin-notice" role="status" data-testid="signin-notice">
            {notice}
          </p>
        )}
        {serverError && (
          <p className="signin-error" role="alert">
            {serverError}
          </p>
        )}
        {!passwordGrant && (
          <p className="signin-error" role="alert">
            This server does not accept password sign-in.
          </p>
        )}
        <label className="field">
          <span>User name</span>
          <input
            name="username"
            autoComplete="username"
            autoFocus={!username}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
            disabled={busy}
          />
        </label>
        <label className="field">
          <span>Password</span>
          <input
            ref={passwordRef}
            name="password"
            type="password"
            autoComplete="current-password"
            autoFocus={!!username}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            disabled={busy}
          />
        </label>
        {error && (
          <p className="signin-error" role="alert" data-testid="signin-error">
            {error}
          </p>
        )}
        <button
          type="submit"
          className="btn btn-primary signin-submit"
          disabled={busy || working || !username.trim() || !password}
        >
          <LogIn size={16} /> {busy || working ? "Signing in…" : "Sign in"}
        </button>
        <p className="signin-foot">
          Your password goes to this server only, once; the page keeps a short-lived token for this tab.
        </p>
      </form>
      <a className="signin-by" href="https://gizmodata.com" target="_blank" rel="noopener noreferrer">
        Built by GizmoData
      </a>
    </div>
  );
}
