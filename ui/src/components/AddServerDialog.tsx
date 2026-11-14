// Adding another Hardcharger server, or signing in to one in the list, on one page (as
// gizmosql-ui's Add server does): saved connections to pick from, the display name, host and
// port (a pasted hardcharger://, chargewire://, jdbc:hardcharger:// or http(s):// URI fills
// them), the sign-in, TLS and how the certificate is trusted, the query timeout, and whether
// to save the connection (never its password, except in the desktop app's keychain). Connect
// goes through the relay: probe the server, register it, sign in, and prove it with a session.
import { BadgeCheck, ChevronDown, Lock, LockOpen, ShieldAlert, UserRound, X } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";

import { SignInError } from "../lib/auth";
import {
  canRememberPasswords,
  forgetRememberedPassword,
  rememberPassword,
  rememberedPassword,
} from "../lib/desktopBridge";
import {
  type ServerAddress,
  addressLabel,
  defaultPort,
  defaultPortFor,
  isGizmoDataCloudHost,
  hostProblem,
  looksLikeUri,
  normalPrefix,
  parseServerUri,
  portProblem,
  serverUrl,
} from "../lib/serverAddress";
import {
  type ProbeResult,
  type RemoteCertificate,
  RemoteError,
  type SavedServer,
  type ServerState,
  type TlsMode,
  serverKey,
  useServers,
} from "../lib/servers";
import { defaultResumeTimeoutS, type ResumeProgress } from "../lib/resuming";
import { useSession } from "../session";
import { ResumeStatus } from "./ResumeStatus";

interface Props {
  /** A server in the list to sign in to (the form starts filled with it); absent: a new one. */
  server?: ServerState;
  onConnected: (serverId: string) => void;
  onClose: () => void;
}

/** Which field an error belongs under. */
type Where = "host" | "auth" | "password" | "tls" | "timeout" | "resume" | "general";
type Problem = { where: Where; message: string };

const tokenHint = "For a scoped token, use the user name token and the token as the password.";

function problemOf(err: unknown, trustChecked: boolean, tls: boolean): Problem {
  if (err instanceof DOMException && err.name === "AbortError") {
    return { where: "general", message: "Stopped waiting for the server to resume." };
  }
  if (err instanceof SignInError) {
    if (err.code === "server_resuming") {
      return {
        where: "resume",
        message: `${err.message}. The server is still starting: try again in a moment, or raise the resume timeout.`,
      };
    }
    if (err.code === "invalid_credentials" || err.status === 401) {
      return { where: "password", message: err.message || "The user name or password is wrong." };
    }
    return { where: "general", message: err.message };
  }
  if (err instanceof RemoteError) {
    switch (err.code) {
      case "remote_unreachable":
        return { where: "host", message: err.message };
      case "remote_not_chargewire":
        return {
          where: "host",
          message: `${err.message}. Only Hardcharger servers can be added here (not GizmoSQL or other Flight SQL servers): check the host, port and path prefix.`,
        };
      case "remote_certificate_untrusted":
        return {
          where: "tls",
          message: trustChecked
            ? err.message
            : `The certificate didn't verify against trusted roots. If you know this server (a private or self-signed certificate), check Trust this server's certificate.`,
        };
      case "remote_certificate_mismatch":
        return { where: "tls", message: err.message };
      case "remote_tls_failed":
        return {
          where: "tls",
          message: tls ? `${err.message}. If the server has no TLS, uncheck Use TLS.` : err.message,
        };
      case "bad_request":
        return { where: "host", message: err.message };
      case "invalid_credentials":
        return { where: "password", message: err.message };
      default:
        return { where: "general", message: err.message };
    }
  }
  return { where: "general", message: err instanceof Error ? err.message : String(err) };
}

export function AddServerDialog({ server, onConnected, onClose }: Props) {
  const session = useSession();
  const snapshot = useServers(session.servers);
  const dialog = useRef<HTMLDialogElement>(null);
  const saved = useMemo(() => snapshot.servers.filter((s) => !s.saved.transient), [snapshot]);
  const canRemember = canRememberPasswords();

  const [selectedId, setSelectedId] = useState<string | undefined>(server?.saved.id);
  const [name, setName] = useState(server?.saved.name ?? "");
  const [host, setHost] = useState(server?.saved.host ?? "");
  const [port, setPort] = useState(String(server?.saved.port ?? defaultPort));
  /** The port is still the dialog's default (nobody typed it, no saved connection or URI named
   * it), so it follows the host: 443 for a GizmoData Cloud host, 31337 otherwise. */
  const [portAuto, setPortAuto] = useState(!server);
  const [prefix, setPrefix] = useState(server?.saved.prefix ?? "");
  const [username, setUsername] = useState(server?.saved.username ?? server?.signIn?.username ?? "");
  const [password, setPassword] = useState("");
  const [useTls, setUseTls] = useState(server?.saved.tls ?? true);
  const [readOnly, setReadOnly] = useState(server?.saved.readOnly ?? false);
  const [trust, setTrust] = useState(server?.saved.trust ?? false);
  /** The fingerprint accepted for this address (from a saved connection, or confirmed here). */
  const [pinned, setPinned] = useState<{ key: string; fingerprint: string } | undefined>(
    server?.saved.fingerprint ? { key: serverKey(server.saved), fingerprint: server.saved.fingerprint } : undefined,
  );
  const [timeout, setTimeoutS] = useState(String(server?.saved.timeout ?? 0));
  const [resumeTimeout, setResumeTimeout] = useState(String(server?.saved.resumeTimeout ?? defaultResumeTimeoutS));
  /** While the sign-in waits for the server to finish resuming: where that stands. */
  const [resuming, setResuming] = useState<ResumeProgress | undefined>();
  /** The server being connected (once signed in), whose own waits the store reports. */
  const [connectingId, setConnectingId] = useState<string | undefined>();
  const stopping = useRef<AbortController | undefined>(undefined);
  const [save, setSave] = useState(true);
  const [remember, setRemember] = useState(false);
  const [chooser, setChooser] = useState(false);
  const [confirming, setConfirming] = useState<{ key: string; certificate: RemoteCertificate; probe: ProbeResult }>();
  const [problem, setProblem] = useState<Problem | undefined>();
  const [oauthOnly, setOauthOnly] = useState<string | undefined>();
  const [working, setWorking] = useState(false);

  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  // A saved connection to sign in to: its remembered password, in the desktop app.
  useEffect(() => {
    if (!server || !canRemember) return;
    let live = true;
    rememberedPassword(server.saved.id).then(
      (pw) => {
        if (live && pw) {
          setPassword(pw);
          setRemember(true);
        }
      },
      () => {},
    );
    return () => {
      live = false;
    };
  }, [server, canRemember]);

  const api = session.servers.api;
  const portNumber = Number(port);
  const address: ServerAddress = { host: host.trim(), port: portNumber, tls: useTls, prefix: normalPrefix(prefix) };
  const key = serverKey(address);
  const mode: TlsMode = !useTls ? "plaintext" : trust ? "fingerprint" : "verified";
  const pinnedHere = pinned?.key === key ? pinned.fingerprint : undefined;

  const clearProblem = () => {
    setProblem(undefined);
    setOauthOnly(undefined);
  };

  const pick = (s: SavedServer) => {
    setSelectedId(s.id);
    setName(s.name);
    setHost(s.host);
    setPort(String(s.port));
    setPortAuto(false);
    setPrefix(s.prefix);
    setUsername(s.username ?? "");
    setPassword("");
    setUseTls(s.tls);
    setReadOnly(s.readOnly ?? false);
    setTrust(s.trust);
    setPinned(s.fingerprint ? { key: serverKey(s), fingerprint: s.fingerprint } : undefined);
    setTimeoutS(String(s.timeout ?? 0));
    setResumeTimeout(String(s.resumeTimeout ?? defaultResumeTimeoutS));
    setSave(true);
    setRemember(false);
    setChooser(false);
    setConfirming(undefined);
    clearProblem();
    if (canRemember) {
      rememberedPassword(s.id).then(
        (pw) => {
          if (pw) {
            setPassword(pw);
            setRemember(true);
          }
        },
        () => {},
      );
    }
  };

  const forget = (s: SavedServer) => {
    session.servers.remove(s.id);
    if (selectedId === s.id) setSelectedId(undefined);
  };

  // A URI pasted (or typed) into Host fills the fields it names.
  const changeHost = (value: string) => {
    clearProblem();
    setConfirming(undefined);
    if (!looksLikeUri(value)) {
      setHost(value);
      if (portAuto) setPort(String(defaultPortFor(value)));
      return;
    }
    const parsed = parseServerUri(value);
    if (!parsed.ok) {
      setHost(value);
      setProblem({ where: "host", message: parsed.error });
      return;
    }
    const a = parsed.address;
    setHost(a.host);
    setPort(String(a.port));
    setPortAuto(!a.portGiven);
    setUseTls(a.tls);
    if (!a.tls) setTrust(false);
    setPrefix(a.prefix);
    if (a.username) setUsername(a.username);
  };

  const fieldProblems = (): Problem | undefined => {
    const h = hostProblem(host);
    if (h) return { where: "host", message: h };
    const p = portProblem(portNumber);
    if (p) return { where: "host", message: p };
    if (!username.trim() || !password) return { where: "password", message: "Enter the user name and password." };
    const t = Number(timeout);
    if (!Number.isInteger(t) || t < 0)
      return { where: "timeout", message: "A timeout is a whole number of seconds, 0 for none." };
    const r = Number(resumeTimeout);
    if (resumeTimeout.trim() === "" || !Number.isInteger(r) || r < 0)
      return { where: "resume", message: "A resume timeout is a whole number of seconds, 0 not to wait." };
    return undefined;
  };

  const connect = async () => {
    const invalid = fieldProblems();
    if (invalid) {
      setProblem(invalid);
      return;
    }
    setWorking(true);
    clearProblem();
    const url = serverUrl(address);
    try {
      // Trusting the certificate: show it first, and pin it once confirmed.
      let fingerprint = mode === "fingerprint" ? pinnedHere : undefined;
      if (mode === "fingerprint" && !fingerprint) {
        if (confirming?.key === key) {
          fingerprint = confirming.certificate.fingerprint;
        } else {
          const shown = await api.probe({ url, tls: "fingerprint" });
          if (!shown.tls) throw new RemoteError("The server presented no certificate.", "remote_tls_failed", 502);
          setConfirming({ key, certificate: shown.tls, probe: shown });
          return;
        }
      }
      const probe = await api.probe({ url, tls: mode, ...(fingerprint ? { fingerprint } : {}) });
      const grants = probe.auth?.grant_types ?? ["password"];
      if (!grants.includes("password")) {
        setOauthOnly(
          grants.includes("oauth_browser")
            ? "This server signs people in only through its identity provider (SSO), which can't be done through this page yet. Ask for a password sign-in, or create a scoped token there and sign in as user token."
            : "This server offers no password sign-in.",
        );
        return;
      }
      const label = name.trim() || probe.server.name || addressLabel(address);
      const timeoutS = Number(timeout);
      const registered = await api.register({
        url,
        name: label,
        tls: mode,
        ...(fingerprint ? { fingerprint } : {}),
      });
      session.servers.relaySessionIssued(registered.relay_session);
      const resumeTimeoutS = Number(resumeTimeout);
      const stop = new AbortController();
      stopping.current = stop;
      const auth = await api.signIn(
        registered.relay_path,
        { grant_type: "password", username: username.trim(), password },
        stop.signal,
        { resumeTimeoutS, onResuming: setResuming },
      );
      setResuming(undefined);
      // In the list only once signed in: a failed attempt leaves nothing behind.
      const entry = session.servers.add(
        {
          name: label,
          ...address,
          trust: mode === "fingerprint",
          ...(fingerprint ? { fingerprint } : {}),
          username: username.trim(),
          readOnly,
          ...(timeoutS > 0 ? { timeout: timeoutS } : {}),
          ...(resumeTimeoutS !== defaultResumeTimeoutS ? { resumeTimeout: resumeTimeoutS } : {}),
          ...(save ? {} : { transient: true }),
        },
        selectedId,
      );
      session.servers.signedIn(entry.id, registered, auth);
      setConnectingId(entry.id);
      // Prove the sign-in with a session, and learn who the server says we are.
      try {
        const conn = await session.servers.connection(entry.id);
        const row: unknown = (await conn.query("SELECT HARDCHARGER_USER() AS u")).get(0)?.toJSON();
        const who = (row as { u?: unknown } | undefined)?.u;
        if (typeof who === "string" && who) session.servers.signedIn(entry.id, registered, { ...auth, username: who });
      } catch (err) {
        session.servers.disconnect(entry.id);
        throw err;
      }
      if (canRemember) {
        if (save && remember) await rememberPassword(entry.id, password).catch(() => {});
        else await forgetRememberedPassword(entry.id).catch(() => {});
      }
      setPassword("");
      setConfirming(undefined);
      onConnected(entry.id);
    } catch (err) {
      setProblem(problemOf(err, trust, useTls));
    } finally {
      stopping.current = undefined;
      setResuming(undefined);
      setConnectingId(undefined);
      setWorking(false);
    }
  };
  // A wait for the server to resume: the sign-in's, or the new connection's (the store's).
  const waiting =
    resuming ?? (connectingId ? snapshot.servers.find((s) => s.saved.id === connectingId)?.resuming : undefined);

  const errorUnder = (where: Where): ReactNode =>
    problem?.where === where ? (
      <p className="field-error" role="alert" data-testid={`add-server-error-${where}`}>
        {problem.message}
      </p>
    ) : null;

  const title = server ? `Sign in to ${server.saved.name || addressLabel(server.saved)}` : "Add a Hardcharger server";
  const savedRows = saved.map((s) => s.saved);

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
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          if (!working) void connect();
        }}
      >
        <div className="dialog-head">
          <h2>{title}</h2>
          <button type="button" className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>
        <div className="dialog-body server-form">
          {problem?.where === "general" && (
            <p className="server-form-error" role="alert" data-testid="add-server-error">
              <ShieldAlert size={14} /> {problem.message}
            </p>
          )}

          {savedRows.length > 0 && (
            <div className="server-field">
              <span className="server-label">Saved connections</span>
              <ul className="saved-connections" aria-label="Saved connections" data-testid="saved-connections">
                {savedRows.map((s) => (
                  <li
                    key={s.id}
                    className={s.id === selectedId ? "saved-connection saved-connection-selected" : "saved-connection"}
                  >
                    <button
                      type="button"
                      className="saved-connection-pick"
                      onClick={() => pick(s)}
                      title="Fill the form with this connection"
                    >
                      <span className="saved-connection-name">{s.name}</span>
                      <span className="saved-connection-detail">
                        {s.username ? `${s.username}@` : ""}
                        {addressLabel(s)}
                      </span>
                    </button>
                    <button
                      type="button"
                      className="saved-connection-forget"
                      onClick={() => forget(s)}
                      aria-label={`Forget ${s.name}`}
                      title="Forget this saved connection (and its remembered password)"
                    >
                      <X size={13} />
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="server-field">
            <label className="server-label" htmlFor="server-name">
              Display name
            </label>
            <div className="server-name-wrap">
              <input
                id="server-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="My Hardcharger server (optional)"
                aria-label="Display name"
                disabled={working}
                autoComplete="off"
              />
              {savedRows.length > 0 && (
                <button
                  type="button"
                  className="server-name-chooser"
                  onClick={() => setChooser((c) => !c)}
                  aria-label="Choose a saved connection"
                  aria-expanded={chooser}
                  title="Choose a saved connection"
                  data-testid="saved-connection-chooser"
                >
                  <UserRound size={14} />
                  <ChevronDown size={12} />
                </button>
              )}
              {chooser && (
                <ul className="server-name-menu" role="menu" aria-label="Saved connections">
                  {savedRows.map((s) => (
                    <li key={s.id}>
                      <button type="button" role="menuitem" onClick={() => pick(s)}>
                        <span className="saved-connection-name">{s.name}</span>
                        <span className="saved-connection-detail">{addressLabel(s)}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>

          <div className="server-row">
            <div className="server-field server-grow-2">
              <label className="server-label" htmlFor="server-host">
                Host <span className="required">*</span>
              </label>
              <input
                id="server-host"
                value={host}
                onChange={(e) => changeHost(e.target.value)}
                placeholder="db.example.com, or paste hardcharger://…"
                aria-label="Host"
                spellCheck={false}
                autoComplete="off"
                disabled={working || !!server}
                aria-invalid={problem?.where === "host"}
              />
            </div>
            <div className="server-field server-grow-1">
              <label className="server-label" htmlFor="server-port">
                Port <span className="required">*</span>
              </label>
              <input
                id="server-port"
                type="number"
                min={1}
                max={65535}
                value={port}
                onChange={(e) => {
                  clearProblem();
                  setPort(e.target.value);
                  setPortAuto(false);
                }}
                aria-label="Port"
                disabled={working || !!server}
              />
              {portAuto && isGizmoDataCloudHost(host) && (
                <span className="server-hint" data-testid="port-cloud-hint">
                  GizmoData Cloud
                </span>
              )}
            </div>
          </div>
          {errorUnder("host")}
          <details className="server-advanced">
            <summary>Advanced</summary>
            <div className="server-field">
              <label className="server-label" htmlFor="server-prefix">
                Path prefix
              </label>
              <input
                id="server-prefix"
                value={prefix}
                onChange={(e) => setPrefix(e.target.value)}
                placeholder="/chargewire"
                aria-label="Path prefix"
                spellCheck={false}
                disabled={working || !!server}
              />
              <span className="server-hint">Where the server serves Chargewire, when a proxy moved it.</span>
            </div>
          </details>

          <div className="server-field">
            <span className="server-label">Authentication</span>
            <div className="server-radios" role="radiogroup" aria-label="Authentication">
              <label className="server-check">
                <input type="radio" name="server-auth" checked readOnly />
                <span>Username &amp; Password</span>
              </label>
              <label
                className="server-check server-disabled"
                title="Browser sign-in to another server isn't supported yet"
              >
                <input type="radio" name="server-auth" disabled />
                <span>SSO / OAuth</span>
              </label>
            </div>
            <span className="server-hint">
              Browser sign-in (SSO) to another server isn&apos;t supported yet: sign in with a scoped token instead.
            </span>
            {oauthOnly && (
              <p className="field-error" role="alert" data-testid="add-server-error-auth">
                {oauthOnly}
              </p>
            )}
          </div>

          <div className="server-row">
            <div className="server-field server-grow-1">
              <label className="server-label" htmlFor="server-username">
                Username <span className="required">*</span>
              </label>
              <input
                id="server-username"
                name="remote-username"
                autoComplete="username"
                value={username}
                onChange={(e) => {
                  clearProblem();
                  setUsername(e.target.value);
                }}
                placeholder="Enter username"
                aria-label="Username"
                disabled={working}
              />
            </div>
            <div className="server-field server-grow-1">
              <label className="server-label" htmlFor="server-password">
                Password <span className="required">*</span>
              </label>
              <input
                id="server-password"
                name="remote-password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => {
                  clearProblem();
                  setPassword(e.target.value);
                }}
                placeholder="Enter password"
                aria-label="Password"
                disabled={working}
                aria-invalid={problem?.where === "password"}
              />
            </div>
          </div>
          <span className="server-hint server-hint-row">{tokenHint}</span>
          {errorUnder("password")}

          <div className="server-row server-checks">
            <label className="server-check">
              <input
                type="checkbox"
                checked={useTls}
                onChange={(e) => {
                  clearProblem();
                  setConfirming(undefined);
                  setUseTls(e.target.checked);
                  if (!e.target.checked) setTrust(false);
                }}
                disabled={working || !!server}
                aria-label="Use TLS"
              />
              <span>Use TLS</span>
            </label>
            <label className={useTls ? "server-check" : "server-check server-disabled"}>
              <input
                type="checkbox"
                checked={trust}
                onChange={(e) => {
                  clearProblem();
                  setConfirming(undefined);
                  setTrust(e.target.checked);
                }}
                disabled={working || !useTls}
                aria-label="Trust this server's certificate"
              />
              <span>Trust this server&apos;s certificate</span>
            </label>
            <span className={`tls-lock tls-lock-${mode}`} data-tls={mode} title={lockText(mode)}>
              {mode === "plaintext" ? <LockOpen size={13} /> : <Lock size={13} />}
            </span>
          </div>
          <span className="server-hint server-hint-row">{lockText(mode)}</span>
          {errorUnder("tls")}
          {confirming?.key === key && (
            <div className="certificate-confirm" data-testid="certificate-confirm">
              <p className="dialog-note dialog-warning">
                <ShieldAlert size={14} /> Check this certificate&apos;s fingerprint with whoever runs the server before
                trusting it.
              </p>
              <dl className="certificate-facts">
                <dt>Server</dt>
                <dd>
                  {confirming.probe.server.name ?? "Chargewire server"} {confirming.probe.server.version ?? ""}
                </dd>
                <dt>Subject</dt>
                <dd>{confirming.certificate.subject}</dd>
                <dt>Issuer</dt>
                <dd>{confirming.certificate.issuer}</dd>
                <dt>Expires</dt>
                <dd>{confirming.certificate.not_after}</dd>
                <dt>SHA-256</dt>
                <dd>
                  <code className="certificate-fingerprint" data-testid="certificate-fingerprint">
                    {confirming.certificate.fingerprint}
                  </code>
                </dd>
              </dl>
            </div>
          )}

          <div className="server-field server-readonly">
            <label className="server-check">
              <input
                type="checkbox"
                checked={readOnly}
                onChange={(e) => setReadOnly(e.target.checked)}
                disabled={working}
              />
              <span>Read-only sessions</span>
            </label>
            <span className="server-hint">
              Start new sessions on this connection in read-only mode. Once connected, read-only stays on for that
              session.
            </span>
          </div>

          <div className="server-field">
            <label className="server-label" htmlFor="server-timeout">
              Query timeout (seconds)
            </label>
            <input
              id="server-timeout"
              type="number"
              min={0}
              value={timeout}
              onChange={(e) => {
                clearProblem();
                setTimeoutS(e.target.value);
              }}
              aria-label="Query timeout (seconds)"
              disabled={working}
            />
            <span className="server-hint">0 = unlimited (no timeout)</span>
            {errorUnder("timeout")}
          </div>

          <div className="server-field">
            <label className="server-label" htmlFor="server-resume-timeout">
              Resume timeout (seconds)
            </label>
            <input
              id="server-resume-timeout"
              type="number"
              min={0}
              value={resumeTimeout}
              onChange={(e) => {
                clearProblem();
                setResumeTimeout(e.target.value);
              }}
              aria-label="Resume timeout (seconds)"
              disabled={working}
            />
            <span className="server-hint">
              How long to wait for a stopped server that starts again on demand (auto-resume); 0 = don't wait
            </span>
            {errorUnder("resume")}
          </div>

          <div className="server-row server-checks">
            <label className="server-check">
              <input
                type="checkbox"
                checked={save}
                onChange={(e) => {
                  setSave(e.target.checked);
                  if (!e.target.checked) setRemember(false);
                }}
                disabled={working}
                aria-label="Save connection for later"
              />
              <span>Save connection for later</span>
            </label>
            <label
              className={canRemember && save ? "server-check" : "server-check server-disabled"}
              title={
                canRemember ? "Kept in your operating system's keychain" : "Available in the Hardcharger desktop app"
              }
            >
              <input
                type="checkbox"
                checked={remember}
                onChange={(e) => setRemember(e.target.checked)}
                disabled={working || !canRemember || !save}
                aria-label="Remember password"
              />
              <span>Remember password</span>
            </label>
          </div>
        </div>
        {waiting && <ResumeStatus progress={waiting} />}
        <div className="dialog-foot">
          {resuming ? (
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => stopping.current?.abort()}
              data-testid="add-server-stop-waiting"
            >
              Stop waiting
            </button>
          ) : (
            <button type="button" className="btn btn-secondary" onClick={onClose} disabled={working}>
              Cancel
            </button>
          )}
          <button type="submit" className="btn btn-primary" disabled={working} data-testid="add-server-connect">
            {confirming?.key === key ? (
              <>
                <BadgeCheck size={14} /> {working ? "Connecting…" : "Trust and connect"}
              </>
            ) : working ? (
              waiting ? (
                "Waiting for the server…"
              ) : (
                "Connecting…"
              )
            ) : (
              "Connect"
            )}
          </button>
        </div>
      </form>
    </dialog>
  );
}

function lockText(mode: TlsMode): string {
  switch (mode) {
    case "verified":
      return "TLS, the certificate verified against trusted roots.";
    case "fingerprint":
      return "TLS, the certificate trusted by its SHA-256 fingerprint (shown before it is pinned).";
    case "plaintext":
      return "Plain HTTP: nothing is encrypted.";
  }
}
