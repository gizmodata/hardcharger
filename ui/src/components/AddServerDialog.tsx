// Adding another Hardcharger server, and signing in to one already in the list: the server's
// name and URL, how its certificate is trusted (verified against trusted roots, or by a
// fingerprint the person accepts after seeing it, like SSH's first connection), then the
// sign-in (a password, or a token) through the relay. Nothing of it is stored but the
// server's name, URL and the accepted fingerprint; the token lives in this tab only.
import { BadgeCheck, KeyRound, Lock, LogIn, ShieldAlert, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { SignInError } from "../lib/auth";
import {
  type ProbeResult,
  type RemoteCertificate,
  RemoteError,
  type ServerState,
  type TlsMode,
  serverLabel,
  urlTlsMode,
} from "../lib/servers";
import { useSession } from "../session";

interface Props {
  /** A server in the list to sign in to (its URL and trust are known); absent: a new one. */
  server?: ServerState;
  onConnected: (serverId: string) => void;
  onClose: () => void;
}

type Step =
  | { kind: "details" }
  | { kind: "trust"; certificate: RemoteCertificate; probe: ProbeResult }
  | { kind: "sign-in"; probe: ProbeResult; tls: TlsMode; fingerprint?: string };

function messageOf(err: unknown): string {
  if (err instanceof RemoteError || err instanceof SignInError) return err.message;
  return err instanceof Error ? err.message : String(err);
}

export function AddServerDialog({ server, onConnected, onClose }: Props) {
  const session = useSession();
  const dialog = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState(server?.saved.name ?? "");
  const [url, setUrl] = useState(server?.saved.url ?? "https://");
  const [step, setStep] = useState<Step>({ kind: "details" });
  const [username, setUsername] = useState(server?.signIn?.username ?? "");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [withToken, setWithToken] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [working, setWorking] = useState(false);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);

  const api = session.servers.api;
  const scheme = urlTlsMode(url);

  // Step 1: find out what the server is, and whether its certificate verifies.
  const probe = async () => {
    const trimmed = url.trim();
    if (!scheme) {
      setError("The URL must start with https:// or http:// (for example https://db.example.com:31337).");
      return;
    }
    setWorking(true);
    setError(undefined);
    try {
      const saved = server?.saved;
      const tls: TlsMode = saved?.tls ?? scheme;
      const fingerprint = saved?.tls === "fingerprint" ? saved.fingerprint : undefined;
      try {
        const result = await api.probe({ url: trimmed, tls, ...(fingerprint ? { fingerprint } : {}) });
        setStep({ kind: "sign-in", probe: result, tls, ...(fingerprint ? { fingerprint } : {}) });
      } catch (err) {
        if (err instanceof RemoteError && err.code === "remote_certificate_untrusted") {
          // Show the certificate, so the person can decide to trust it.
          const shown = await api.probe({ url: trimmed, tls: "fingerprint" });
          if (shown.tls) setStep({ kind: "trust", certificate: shown.tls, probe: shown });
          else setError(messageOf(err));
        } else {
          throw err;
        }
      }
    } catch (err) {
      setError(messageOf(err));
    } finally {
      setWorking(false);
    }
  };

  // Step 3: register the remote with the relay and sign in to it.
  const signIn = async (current: Extract<Step, { kind: "sign-in" }>) => {
    setWorking(true);
    setError(undefined);
    try {
      const trimmed = url.trim();
      const label = name.trim() || current.probe.server.name || new URL(trimmed).host;
      const saved = session.servers.add({
        name: label,
        url: trimmed,
        tls: current.tls,
        ...(current.fingerprint ? { fingerprint: current.fingerprint } : {}),
      });
      const registered = await api.register({
        url: trimmed,
        name: label,
        tls: current.tls,
        ...(current.fingerprint ? { fingerprint: current.fingerprint } : {}),
      });
      session.servers.relaySessionIssued(registered.relay_session);
      let auth;
      if (withToken || browserOnly) {
        // A token is checked by using it: the sign-in endpoint takes no token grant.
        auth = { username: "", token: token.trim(), expiresAt: Date.now() + 24 * 3600_000 };
      } else {
        auth = await api.signIn(registered.relay_path, { grant_type: "password", username: username.trim(), password });
      }
      session.servers.signedIn(saved.id, registered, auth);
      // Prove the sign-in with a session, and learn who the server says we are.
      try {
        const conn = await session.servers.connection(saved.id);
        const row: unknown = (await conn.query("SELECT HARDCHARGER_USER() AS u")).get(0)?.toJSON();
        const who = (row as { u?: unknown } | undefined)?.u;
        if (typeof who === "string" && who) session.servers.signedIn(saved.id, registered, { ...auth, username: who });
      } catch (err) {
        session.servers.disconnect(saved.id);
        throw err;
      }
      setPassword("");
      setToken("");
      onConnected(saved.id);
    } catch (err) {
      setError(messageOf(err));
    } finally {
      setWorking(false);
    }
  };

  const grants = step.kind === "sign-in" ? (step.probe.auth?.grant_types ?? ["password"]) : [];
  const passwordGrant = grants.includes("password");
  const browserOnly = step.kind === "sign-in" && !passwordGrant && grants.includes("oauth_browser");
  const title = server ? `Sign in to ${serverLabel(server)}` : "Add a server";

  return (
    <dialog
      ref={dialog}
      className="dialog dialog-server"
      onClose={onClose}
      aria-label={title}
      data-testid="add-server-dialog"
    >
      <form
        method="dialog"
        onSubmit={(e) => {
          e.preventDefault();
          if (working) return;
          if (step.kind === "details") void probe();
          else if (step.kind === "sign-in") void signIn(step);
        }}
      >
        <div className="dialog-head">
          <h2>{title}</h2>
          <button type="button" className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>
        <div className="dialog-body">
          {step.kind === "details" && (
            <>
              <label className="field">
                <span>Name</span>
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Warehouse"
                  aria-label="Name"
                  autoFocus={!server}
                  disabled={working}
                />
              </label>
              <label className="field">
                <span>Server URL</span>
                <input
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="https://db.example.com:31337"
                  aria-label="Server URL"
                  spellCheck={false}
                  required
                  disabled={working || !!server}
                />
              </label>
              <p className="dialog-note">
                {scheme === "plaintext" ? (
                  <>
                    <ShieldAlert size={13} /> Plain HTTP: the connection to this server is not encrypted.
                  </>
                ) : (
                  <>
                    <Lock size={13} /> TLS: the certificate is verified against trusted roots; a private or self-signed
                    certificate can be trusted by its fingerprint next.
                  </>
                )}
              </p>
            </>
          )}
          {step.kind === "trust" && (
            <>
              <p className="dialog-note dialog-warning">
                <ShieldAlert size={14} /> This server's certificate does not chain to a trusted root. Check its
                fingerprint with whoever runs the server before trusting it.
              </p>
              <dl className="certificate-facts">
                <dt>Server</dt>
                <dd>
                  {step.probe.server.name ?? "Chargewire server"} {step.probe.server.version ?? ""}
                </dd>
                <dt>Subject</dt>
                <dd>{step.certificate.subject}</dd>
                <dt>Issuer</dt>
                <dd>{step.certificate.issuer}</dd>
                <dt>Names</dt>
                <dd>{step.certificate.names.join(", ") || "none"}</dd>
                <dt>Expires</dt>
                <dd>{step.certificate.not_after}</dd>
                <dt>SHA-256</dt>
                <dd>
                  <code className="certificate-fingerprint" data-testid="certificate-fingerprint">
                    {step.certificate.fingerprint}
                  </code>
                </dd>
              </dl>
            </>
          )}
          {step.kind === "sign-in" && (
            <>
              <p className="dialog-note">
                {step.tls === "plaintext" ? <ShieldAlert size={13} /> : <Lock size={13} />}{" "}
                {step.probe.server.name ?? "Chargewire server"} {step.probe.server.version ?? ""} at {step.probe.origin}
                {step.tls === "fingerprint" ? " (certificate trusted by its fingerprint)" : ""}
              </p>
              {browserOnly && (
                <p className="signin-error" role="alert">
                  This server signs people in through its identity provider in a browser, which can't be done through
                  the relay yet. Sign in with a token instead: create one on that server (CREATE SCOPED TOKEN) and paste
                  it below.
                </p>
              )}
              <label className="field field-check">
                <input
                  type="checkbox"
                  checked={withToken || browserOnly}
                  disabled={browserOnly}
                  onChange={(e) => setWithToken(e.target.checked)}
                />
                <span>Sign in with a token</span>
              </label>
              {withToken || browserOnly ? (
                <label className="field">
                  <span>Access token</span>
                  <input
                    type="password"
                    autoComplete="off"
                    value={token}
                    onChange={(e) => setToken(e.target.value)}
                    aria-label="Access token"
                    autoFocus
                    disabled={working}
                  />
                </label>
              ) : (
                <>
                  <label className="field">
                    <span>User name</span>
                    <input
                      name="remote-username"
                      autoComplete="username"
                      value={username}
                      onChange={(e) => setUsername(e.target.value)}
                      aria-label="User name"
                      autoFocus={!username}
                      disabled={working}
                    />
                  </label>
                  <label className="field">
                    <span>Password</span>
                    <input
                      name="remote-password"
                      type="password"
                      autoComplete="current-password"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      aria-label="Password"
                      autoFocus={!!username}
                      disabled={working}
                    />
                  </label>
                </>
              )}
            </>
          )}
          {error && (
            <p className="signin-error" role="alert" data-testid="add-server-error">
              {error}
            </p>
          )}
        </div>
        <div className="dialog-foot">
          <button type="button" className="btn btn-secondary" onClick={onClose} disabled={working}>
            Cancel
          </button>
          {step.kind === "details" && (
            <button
              type="submit"
              className="btn btn-primary"
              disabled={working || !url.trim()}
              data-testid="add-server-next"
            >
              {working ? "Connecting…" : "Connect"}
            </button>
          )}
          {step.kind === "trust" && (
            <button
              type="button"
              className="btn btn-primary"
              data-testid="trust-certificate"
              onClick={() =>
                setStep({
                  kind: "sign-in",
                  probe: step.probe,
                  tls: "fingerprint",
                  fingerprint: step.certificate.fingerprint,
                })
              }
            >
              <BadgeCheck size={14} /> Trust this certificate
            </button>
          )}
          {step.kind === "sign-in" && (
            <button
              type="submit"
              className="btn btn-primary"
              disabled={working || (withToken || browserOnly ? !token.trim() : !username.trim() || !password)}
              data-testid="add-server-sign-in"
            >
              {withToken || browserOnly ? <KeyRound size={14} /> : <LogIn size={14} />}{" "}
              {working ? "Signing in…" : "Sign in"}
            </button>
          )}
        </div>
      </form>
    </dialog>
  );
}
