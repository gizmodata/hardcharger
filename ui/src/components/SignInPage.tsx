import type { DiscoveryDocument } from "@gizmodata/warp-sql";
import { BadgeCheck, LogIn, Moon, Sun } from "lucide-react";
import { type SyntheticEvent, useRef, useState } from "react";

import { type SignedIn, SignInError, lastUsername, signIn, signInWithCertificate } from "../lib/auth";
import type { Theme } from "../lib/theme";
import { ByGizmoData, TrademarkNotice, Wordmark } from "./Brand";

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
  const grants = server?.auth?.grant_types;
  const passwordGrant = grants?.includes("password") ?? true;
  // Offered by a server with mutual TLS and certificate sign-in on (Warp SQL §4.4): the
  // browser has already presented its certificate to load this page.
  const certificateGrant = grants?.includes("client_certificate") ?? false;

  const run = async (attempt: () => Promise<SignedIn>, afterFailure?: () => void) => {
    if (working) return;
    setWorking(true);
    setError(undefined);
    try {
      const auth = await attempt();
      setPassword("");
      onSignedIn(auth);
    } catch (err) {
      setError(err instanceof SignInError ? err.message : String(err));
      afterFailure?.();
    } finally {
      setWorking(false);
    }
  };

  const submit = (e: SyntheticEvent<HTMLFormElement>) => {
    e.preventDefault();
    void run(
      () => signIn(username.trim(), password),
      () => {
        setPassword("");
        passwordRef.current?.focus();
      },
    );
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
      <form className="signin-card" onSubmit={submit} aria-label="Sign in">
        <h1 className="signin-brand">
          <Wordmark size="signin" />
        </h1>
        <p className="signin-tagline">Fast, secure, remote DuckDB</p>
        <p className="signin-server" data-testid="server-version">
          {server
            ? `${server.server?.name ?? "Warp SQL server"} ${server.server?.version ?? ""}`.trim()
            : serverError
              ? "Server not reachable"
              : "Connecting…"}
        </p>
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
        {server && !passwordGrant && !certificateGrant && (
          <p className="signin-error" role="alert">
            This server accepts no sign-in this page can offer.
          </p>
        )}
        {passwordGrant && (
          <>
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
          </>
        )}
        {error && (
          <p className="signin-error" role="alert" data-testid="signin-error">
            {error}
          </p>
        )}
        {passwordGrant && (
          <button
            type="submit"
            className="btn btn-primary signin-submit"
            disabled={busy || working || !username.trim() || !password}
          >
            <LogIn size={16} /> {busy || working ? "Signing in…" : "Sign in"}
          </button>
        )}
        {certificateGrant && (
          <button
            type="button"
            className={`btn ${passwordGrant ? "btn-secondary" : "btn-primary"} signin-submit`}
            disabled={busy || working}
            onClick={() => void run(() => signInWithCertificate())}
          >
            <BadgeCheck size={16} /> Sign in with my certificate
          </button>
        )}
        <p className="signin-foot">
          {passwordGrant
            ? "Your password goes to this server only, once; the page keeps a short-lived token for this tab."
            : "The page keeps a short-lived token for this tab, usable only with your certificate."}
        </p>
      </form>
      <footer className="signin-footer">
        <ByGizmoData />
        <TrademarkNotice />
      </footer>
    </div>
  );
}
