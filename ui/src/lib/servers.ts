// Other Hardcharger servers in this UI: the list a person keeps (name, URL, how its
// certificate is trusted; never a password or token), kept in this browser's localStorage,
// and the sign-ins of this tab (the remote's token, the relay's id for it), kept in
// sessionStorage like the UI's own sign-in. The browser can't reach a second server itself,
// so every request to a remote goes to the relay on the UI's own origin
// (/ui/v1/remotes/<id>/chargewire/..., server/src/web_relay.cc), which forwards it.
//
// A relay session is what lets this tab use the relay: on a Hardcharger server it is the
// UI's own sign-in (the bearer token); where there is no server behind the UI
// (`hardcharger ui`, the desktop app without its local server) the relay issues one when
// the first remote is registered, and it stands once a sign-in to a remote succeeded. The
// UI keeps whichever it has (Chargewire-Relay-Session) and sends both.

import { type Connection, type DiscoveryDocument, connect } from "@gizmodata/chargewire";
import { useSyncExternalStore } from "react";

import { type SignedIn, SignInError } from "./auth";
import { forgetRememberedPassword, removeDesktopServer, renameDesktopServer } from "./desktopBridge";
import {
  type ServerAddress,
  addressLabel,
  defaultPort,
  normalPrefix,
  parseServerUri,
  serverUrl,
} from "./serverAddress";
import type { ConnectionOptions } from "./chargewire";
import { pageRequests } from "./pageRequests";
import { defaultResumeTimeoutS, type ResumeOptions, type ResumeProgress, sendWhileResuming } from "./resuming";

export type TlsMode = "verified" | "fingerprint" | "plaintext";

/** The UI's own server, as a server id ("" everywhere a server is named). */
export const localServerId = "";

/**
 * A server the person added (what localStorage keeps, and the shape the desktop app hands
 * over from its Settings > Remote servers): its address, how its certificate is trusted, the
 * user name to fill in and the query timeout. Never a password or token.
 */
export interface SavedServer extends ServerAddress {
  id: string;
  name: string;
  /** Trust the certificate by its SHA-256 fingerprint (a private or self-signed one). */
  trust: boolean;
  /** With trust: the fingerprint the person accepted (aa:bb:...). */
  fingerprint?: string;
  username?: string;
  /** Seconds before the server cancels a statement; 0 or absent: none. */
  timeout?: number;
  /**
   * Seconds to wait for the server while it is resuming (a stopped GizmoData Cloud cluster with
   * auto-resume starting again, Chargewire §11.2); absent: 600, 0: don't wait.
   */
  resumeTimeout?: number;
  /** Not saved for later (Save connection for later off): kept for this tab only. */
  transient?: boolean;
}

/** How a saved server's connection is protected (the relay's TLS mode, and the lock). */
export function tlsModeOf(saved: Pick<SavedServer, "tls" | "trust">): TlsMode {
  return !saved.tls ? "plaintext" : saved.trust ? "fingerprint" : "verified";
}

/** The URL the relay is given for a saved server. */
export function urlOf(saved: SavedServer): string {
  return serverUrl(saved);
}

/** One server, however it was saved: host, port and base path. */
export function serverKey(address: ServerAddress): string {
  return `${address.host.toLowerCase()}|${address.port}|${normalPrefix(address.prefix)}`;
}

/** What the relay learned about a remote's certificate. */
export interface RemoteCertificate {
  mode: TlsMode;
  fingerprint: string;
  subject: string;
  issuer: string;
  not_after: string;
  names: string[];
  verified: boolean;
}

export interface ProbeResult {
  url: string;
  origin: string;
  base_path: string;
  tls_mode: TlsMode;
  tls: RemoteCertificate | null;
  server: { name?: string; version?: string };
  auth: { grant_types?: string[]; oauth_browser?: { provider?: string } } | null;
  capabilities: string[];
}

export interface RegisteredRemote {
  id: string;
  name: string;
  url: string;
  origin: string;
  tls_mode: TlsMode;
  tls: RemoteCertificate | null;
  server: { name?: string; version?: string };
  relay_session: string;
  relay_path: string;
  signed_in: boolean;
}

/** This tab's sign-in to a saved server (what sessionStorage keeps). */
export interface RemoteSignIn {
  serverId: string;
  relayId: string;
  relayPath: string;
  token: string;
  expiresAt: number;
  username: string;
  serverName: string;
  version: string;
  tls: RemoteCertificate | null;
}

/** The UI's contract with the program serving it (GET /ui/v1/app). */
export interface AppContract {
  version: string;
  /** The serving program is a Hardcharger server people sign in to ("This server"). */
  local_server: boolean;
  /** The relay to other servers is offered. */
  remote_servers: boolean;
}

export class RemoteError extends Error {
  override readonly name = "RemoteError";
  constructor(
    message: string,
    readonly code: string,
    readonly status: number,
  ) {
    super(message);
  }
}

// ---- Storage ----

const serversKey = (user: string) => `hardcharger-ui:servers:v1:${user}`;
const signInsKey = "hardcharger-ui:remote-sign-ins:v1";
const relaySessionKey = "hardcharger-ui:relay-session:v1";
/** Servers added without Save connection for later: this tab only. */
const tabServersKey = "hardcharger-ui:tab-servers:v1";
/** The desktop app hands its saved servers to the workspace under this key (sessionStorage). */
export const desktopServersKey = "hardcharger-ui:desktop-servers";

function local(): Storage | undefined {
  try {
    return window.localStorage;
  } catch {
    return undefined;
  }
}

function session(): Storage | undefined {
  try {
    return window.sessionStorage;
  } catch {
    return undefined;
  }
}

/**
 * A stored entry as a SavedServer, or undefined when it isn't one. Entries saved before the
 * dialog took a host and port (a `url` and a `tls` of verified / fingerprint / plaintext, the
 * first 2.7.0 builds) are read the same way.
 */
export function normalizeSaved(s: unknown): SavedServer | undefined {
  if (s === null || typeof s !== "object") return undefined;
  const r = s as Record<string, unknown>;
  if (typeof r["id"] !== "string" || typeof r["name"] !== "string") return undefined;
  const text = (key: string): string | undefined => {
    const value = r[key];
    return typeof value === "string" ? value : undefined;
  };
  let address: ServerAddress;
  let trust = r["trust"] === true;
  if (typeof r["host"] === "string" && r["host"]) {
    const port = typeof r["port"] === "number" ? r["port"] : defaultPort;
    if (!Number.isInteger(port) || port < 1 || port > 65535) return undefined;
    address = { host: r["host"], port, tls: r["tls"] !== false, prefix: normalPrefix(text("prefix") ?? "") };
  } else if (typeof r["url"] === "string") {
    const parsed = parseServerUri(r["url"]);
    if (!parsed.ok) return undefined;
    const mode = r["tls"];
    if (mode !== "verified" && mode !== "fingerprint" && mode !== "plaintext") return undefined;
    const { host, port } = parsed.address; // only the address: not portGiven, username or catalog
    address = { host, port, tls: mode !== "plaintext", prefix: parsed.address.prefix };
    trust = mode === "fingerprint";
  } else {
    return undefined;
  }
  const fingerprint = text("fingerprint");
  const username = text("username");
  const timeout = typeof r["timeout"] === "number" && r["timeout"] > 0 ? Math.floor(r["timeout"]) : undefined;
  const resume = r["resumeTimeout"];
  const resumeTimeout =
    typeof resume === "number" && Number.isFinite(resume) && resume >= 0 ? Math.floor(resume) : undefined;
  return {
    id: r["id"],
    name: r["name"],
    ...address,
    trust: address.tls && trust,
    ...(fingerprint ? { fingerprint } : {}),
    ...(username ? { username } : {}),
    ...(timeout ? { timeout } : {}),
    ...(resumeTimeout !== undefined ? { resumeTimeout } : {}),
  };
}

export function loadSavedServers(user: string, storage: Storage | undefined = local()): SavedServer[] {
  try {
    const raw = storage?.getItem(serversKey(user));
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.flatMap((item) => normalizeSaved(item) ?? []) : [];
  } catch {
    return [];
  }
}

export function saveSavedServers(
  user: string,
  servers: readonly SavedServer[],
  storage: Storage | undefined = local(),
): void {
  try {
    // Only the fields the list is for: never anything a caller may have left on an entry.
    const kept = servers.map(
      ({ id, name, host, port, tls, prefix, trust, fingerprint, username, timeout, resumeTimeout }) => ({
        id,
        name,
        host,
        port,
        tls,
        prefix,
        trust,
        ...(fingerprint ? { fingerprint } : {}),
        ...(username ? { username } : {}),
        ...(timeout ? { timeout } : {}),
        ...(resumeTimeout !== undefined ? { resumeTimeout } : {}),
      }),
    );
    storage?.setItem(serversKey(user), JSON.stringify(kept.filter((_, i) => !servers[i]!.transient)));
  } catch {
    // not remembered (a private window, or storage blocked)
  }
}

/** Servers the desktop app handed over, merged with the saved list by URL (the saved entry wins). */
// Servers removed from the list, by serverKey(): the desktop app hands its saved list over
// every time the workspace opens (and it stays in the tab's sessionStorage), so without this a
// removed server would come back at the next load. Adding the server again clears it.
const dismissedKey = (user: string) => `hardcharger-ui:dismissed-servers:v1:${user}`;

export function loadDismissed(user: string, storage: Storage | undefined = local()): Set<string> {
  try {
    const raw = storage?.getItem(dismissedKey(user));
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(parsed) ? parsed.filter((k): k is string => typeof k === "string") : []);
  } catch {
    return new Set();
  }
}

export function saveDismissed(user: string, keys: ReadonlySet<string>, storage: Storage | undefined = local()): void {
  try {
    if (keys.size === 0) storage?.removeItem(dismissedKey(user));
    else storage?.setItem(dismissedKey(user), JSON.stringify([...keys]));
  } catch {
    // a removal lasts until the page reloads
  }
}

export function mergeDesktopServers(
  saved: readonly SavedServer[],
  storage: Storage | undefined = session(),
  dismissed: ReadonlySet<string> = new Set(),
): SavedServer[] {
  let handed: unknown;
  try {
    const raw = storage?.getItem(desktopServersKey);
    if (!raw) return [...saved];
    handed = JSON.parse(raw);
  } catch {
    return [...saved];
  }
  if (!Array.isArray(handed)) return [...saved];
  const out = [...saved];
  const known = new Set(saved.map((s) => serverKey(s)));
  for (const raw of handed) {
    const item = normalizeSaved(
      raw && typeof raw === "object" && !(raw as { id?: unknown }).id ? { ...raw, id: newServerId() } : raw,
    );
    if (!item || known.has(serverKey(item)) || dismissed.has(serverKey(item))) continue;
    known.add(serverKey(item));
    out.push(item);
  }
  return out;
}

/** The servers this tab added without saving them. */
export function loadTabServers(storage: Storage | undefined = session()): SavedServer[] {
  try {
    const raw = storage?.getItem(tabServersKey);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed)
      ? parsed.flatMap((item) => normalizeSaved(item) ?? []).map((s) => ({ ...s, transient: true }))
      : [];
  } catch {
    return [];
  }
}

export function saveTabServers(servers: readonly SavedServer[], storage: Storage | undefined = session()): void {
  try {
    storage?.setItem(tabServersKey, JSON.stringify(servers.filter((s) => s.transient)));
  } catch {
    // they last until the page reloads
  }
}

export function loadRemoteSignIns(storage: Storage | undefined = session()): RemoteSignIn[] {
  try {
    const raw = storage?.getItem(signInsKey);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((s): s is RemoteSignIn => {
      const r = s as Record<string, unknown>;
      return (
        typeof r["serverId"] === "string" &&
        typeof r["relayId"] === "string" &&
        typeof r["relayPath"] === "string" &&
        typeof r["token"] === "string" &&
        typeof r["expiresAt"] === "number" &&
        r["expiresAt"] - Date.now() > 5000
      );
    });
  } catch {
    return [];
  }
}

export function saveRemoteSignIns(signIns: readonly RemoteSignIn[], storage: Storage | undefined = session()): void {
  try {
    storage?.setItem(signInsKey, JSON.stringify(signIns));
  } catch {
    // the sign-ins last until the page reloads
  }
}

export function loadRelaySession(storage: Storage | undefined = session()): string | undefined {
  try {
    return storage?.getItem(relaySessionKey) ?? undefined;
  } catch {
    return undefined;
  }
}

export function saveRelaySession(key: string | undefined, storage: Storage | undefined = session()): void {
  try {
    if (key) storage?.setItem(relaySessionKey, key);
    else storage?.removeItem(relaySessionKey);
  } catch {
    // kept in memory only
  }
}

export function newServerId(): string {
  return typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

// ---- The relay's API ----

interface Problem {
  code?: string;
  detail?: string;
  title?: string;
}

/** The relay's own endpoints, with this tab's relay credentials. */
export class RelayApi {
  constructor(
    private readonly auth: () => { token?: string; relaySession?: string },
    private readonly base: string = `${window.location.origin}/ui/v1`,
  ) {}

  headers(extra: Record<string, string> = {}): Record<string, string> {
    const { token, relaySession } = this.auth();
    const headers: Record<string, string> = { ...extra };
    if (token) headers["Authorization"] = `Bearer ${token}`;
    if (relaySession) headers["Chargewire-Relay-Session"] = relaySession;
    return headers;
  }

  async contract(signal?: AbortSignal): Promise<AppContract> {
    const resp = await pageRequests.fetch(`${this.base}/app`, {
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal: signal ?? null,
    });
    if (!resp.ok)
      throw new RemoteError(`the server answered ${resp.status} for the UI's contract`, "contract", resp.status);
    return (await resp.json()) as AppContract;
  }

  probe(body: { url: string; tls?: TlsMode; fingerprint?: string }, signal?: AbortSignal): Promise<ProbeResult> {
    return this.call<ProbeResult>("POST", "/remotes/probe", body, signal);
  }

  register(
    body: { url: string; name?: string; tls?: TlsMode; fingerprint?: string },
    signal?: AbortSignal,
  ): Promise<RegisteredRemote> {
    return this.call<RegisteredRemote>("POST", "/remotes", body, signal);
  }

  list(signal?: AbortSignal): Promise<{ remotes: RegisteredRemote[]; relay_session: string; signed_in: boolean }> {
    return this.call("GET", "/remotes", undefined, signal);
  }

  async remove(relayId: string): Promise<void> {
    await this.call<unknown>("DELETE", `/remotes/${encodeURIComponent(relayId)}`, undefined, undefined, true);
  }

  /**
   * Signs in to the remote through the relay (a password, or a token checked by a session).
   * While the remote is resuming (server_resuming, §11.2) the sign-in is sent again as it
   * asks, until `resume.resumeTimeoutS` (default 600), telling `resume.onResuming` each wait.
   */
  async signIn(
    relayPath: string,
    grant: Record<string, string>,
    signal?: AbortSignal,
    resume: Omit<ResumeOptions, "signal"> = {},
  ): Promise<SignedIn> {
    let resp: Response;
    try {
      resp = await sendWhileResuming(
        () =>
          pageRequests.fetch(`${window.location.origin}${relayPath}/v1/auth/token`, {
            method: "POST",
            headers: this.headers({ "Content-Type": "application/json", Accept: "application/json" }),
            body: JSON.stringify(grant),
            cache: "no-store",
            signal: signal ?? null,
          }),
        { ...resume, signal },
      );
    } catch (err) {
      if (signal?.aborted) throw err;
      throw new SignInError("The server could not be reached.", "client_transport_error", undefined);
    }
    const body = (await resp.json().catch(() => ({}))) as Problem & { access_token?: string; expires_in?: number };
    if (!resp.ok) {
      throw new SignInError(
        body.detail ?? body.title ?? `sign-in failed (HTTP ${resp.status})`,
        body.code ?? "client_sign_in_failed",
        resp.status,
      );
    }
    if (!body.access_token)
      throw new SignInError("The server sent no access token.", "client_protocol_error", resp.status);
    const lifetime = typeof body.expires_in === "number" ? body.expires_in : 3600;
    return { username: grant["username"] ?? "", token: body.access_token, expiresAt: Date.now() + lifetime * 1000 };
  }

  private async call<T>(
    method: string,
    path: string,
    body?: unknown,
    signal?: AbortSignal,
    noContent = false,
  ): Promise<T> {
    let resp: Response;
    try {
      resp = await pageRequests.fetch(`${this.base}${path}`, {
        method,
        headers: this.headers({
          Accept: "application/json",
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        }),
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        cache: "no-store",
        signal: signal ?? null,
      });
    } catch (err) {
      if (signal?.aborted) throw err;
      throw new RemoteError("This server could not be reached.", "client_transport_error", 0);
    }
    if (noContent && resp.status === 204) return undefined as T;
    const parsed = (await resp.json().catch(() => ({}))) as Problem;
    if (!resp.ok) {
      throw new RemoteError(
        parsed.detail ?? parsed.title ?? `the relay answered ${resp.status}`,
        parsed.code ?? "relay_error",
        resp.status,
      );
    }
    return parsed as T;
  }
}

// ---- Connections through the relay ----

/**
 * A fetch for a remote's Chargewire calls: the client puts the remote's token in
 * Authorization; the relay wants it in Chargewire-Relay-Authorization and this tab's relay
 * credentials in Authorization / Chargewire-Relay-Session.
 */
export function relayFetch(auth: () => { token?: string; relaySession?: string }): typeof fetch {
  return (input, init) => {
    const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
    const remote = headers.get("Authorization");
    headers.delete("Authorization");
    if (remote) headers.set("Chargewire-Relay-Authorization", remote);
    const { token, relaySession } = auth();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    if (relaySession) headers.set("Chargewire-Relay-Session", relaySession);
    return pageRequests.fetch(input, { ...init, headers });
  };
}

/** A connection (one session) to a remote, through the relay. */
export function openRemoteConnection(
  signIn: RemoteSignIn,
  relayAuth: () => { token?: string; relaySession?: string },
  options: ConnectionOptions & {
    timeoutS?: number;
    resumeTimeoutS?: number | undefined;
    onResuming?: ((progress: ResumeProgress) => void) | undefined;
  } = {},
): Promise<Connection> {
  const { signal, catalog, schema, reconnect, onResuming } = options;
  const timeoutS = options.timeoutS ?? 0;
  return connect({
    url: `${window.location.origin}${signIn.relayPath}`,
    // The saved connection's query timeout: the server cancels a statement after it (§6.9).
    ...(timeoutS > 0 ? { statementTimeoutMs: timeoutS * 1000 } : {}),
    // Waiting for a remote that is resuming (§11.2), and telling the page while it does.
    resumeTimeout: options.resumeTimeoutS ?? defaultResumeTimeoutS,
    ...(onResuming ? { onResuming: (e) => onResuming({ ...e, at: Date.now() }) } : {}),
    fetch: relayFetch(relayAuth),
    token: signIn.token,
    ...(catalog ? { catalog } : {}),
    ...(schema ? { schema } : {}),
    ...(reconnect === undefined ? {} : { reconnect }),
    resultPartitions: 1,
    queryTag: { client: "hardcharger-ui", ...options.queryTag },
    logger: { warn: (message: string) => console.info(`[chargewire] ${message}`) },
    ...(signal ? { signal } : {}),
  });
}

// ---- The store ----

export type ServerStatus = "disconnected" | "connecting" | "connected" | "error";

export interface ServerState {
  saved: SavedServer;
  status: ServerStatus;
  error?: string;
  signIn?: RemoteSignIn;
  /** While a request to it waits for it to finish resuming (§11.2): where that wait stands. */
  resuming?: ResumeProgress;
}

export interface ServersSnapshot {
  servers: readonly ServerState[];
  /** The relay session this tab holds (the one the relay issued, where there is no sign-in). */
  relaySession: string | undefined;
}

/**
 * The servers this tab knows, their sign-ins, and the connections it opens to them. One per
 * signed-in session (or per page in the UI without a server); components subscribe with
 * useServers().
 */
export class ServersStore {
  readonly api: RelayApi;
  #state: ServersSnapshot;
  readonly #user: string;
  readonly #localToken: () => string | undefined;
  readonly #listeners = new Set<() => void>();
  /** The metadata connection of each connected server (the schema browser's listings). */
  readonly #connections = new Map<string, Promise<Connection>>();
  /** Every session this tab opened on a remote (an editor tab's, an upload's), by server id. */
  readonly #extra = new Map<Connection, string>();

  constructor(user: string, localToken: () => string | undefined) {
    this.#user = user;
    this.#localToken = localToken;
    this.api = new RelayApi(() => this.relayAuth());
    const saved = mergeDesktopServers(loadSavedServers(user), undefined, loadDismissed(user));
    saveSavedServers(user, saved);
    const keys = new Set(saved.map(serverKey));
    for (const s of loadTabServers()) if (!keys.has(serverKey(s))) saved.push(s);
    const signIns = new Map(loadRemoteSignIns().map((s) => [s.serverId, s]));
    this.#state = {
      servers: saved.map((s) => {
        const signIn = signIns.get(s.id);
        return signIn ? { saved: s, status: "connected", signIn } : { saved: s, status: "disconnected" };
      }),
      relaySession: loadRelaySession(),
    };
  }

  relayAuth(): { token?: string; relaySession?: string } {
    const token = this.#localToken();
    const relaySession = this.#state.relaySession;
    return { ...(token ? { token } : {}), ...(relaySession ? { relaySession } : {}) };
  }

  snapshot(): ServersSnapshot {
    return this.#state;
  }

  subscribe(listener: () => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  #set(next: ServersSnapshot): void {
    this.#state = next;
    saveSavedServers(
      this.#user,
      next.servers.map((s) => s.saved),
    );
    saveTabServers(next.servers.map((s) => s.saved));
    saveRemoteSignIns(next.servers.flatMap((s) => (s.signIn ? [s.signIn] : [])));
    saveRelaySession(next.relaySession);
    for (const l of this.#listeners) l();
  }

  /**
   * Shows (or, with undefined, clears) where a wait for the server to finish resuming stands.
   * Not stored anywhere: it only tells the page.
   */
  resuming(id: string, progress: ResumeProgress | undefined): void {
    const current = this.find(id);
    if (!current || (!progress && !current.resuming)) return;
    this.#state = {
      ...this.#state,
      servers: this.#state.servers.map((s) => {
        if (s.saved.id !== id) return s;
        const { resuming: _was, ...rest } = s;
        return progress ? { ...rest, resuming: progress } : rest;
      }),
    };
    for (const l of this.#listeners) l();
  }

  #patch(id: string, patch: Partial<ServerState>): void {
    this.#set({
      ...this.#state,
      servers: this.#state.servers.map((s) => (s.saved.id === id ? { ...s, ...patch } : s)),
    });
  }

  find(id: string): ServerState | undefined {
    return this.#state.servers.find((s) => s.saved.id === id);
  }

  /** The servers this tab is signed in to. */
  connected(): ServerState[] {
    return this.#state.servers.filter((s) => s.status === "connected" && s.signIn);
  }

  /** Remembers a server (not yet signed in). Returns it (an existing one with the URL is reused). */
  /**
   * Remembers a server (not yet signed in) and returns it. One entry per server: an entry with
   * the same address is reused, and so is `replaceId` (the saved connection the dialog was
   * filled from, whose address the person edited), so one server never shows twice.
   */
  add(server: Omit<SavedServer, "id">, replaceId?: string): SavedServer {
    const key = serverKey(server);
    const dismissed = loadDismissed(this.#user);
    if (dismissed.delete(key)) saveDismissed(this.#user, dismissed);
    const existing =
      this.#state.servers.find((s) => serverKey(s.saved) === key) ??
      (replaceId ? this.#state.servers.find((s) => s.saved.id === replaceId && !s.signIn) : undefined);
    if (existing) {
      const { transient: _was, ...kept } = existing.saved;
      const saved: SavedServer = { ...kept, ...server };
      this.#patch(existing.saved.id, { saved });
      return saved;
    }
    const saved: SavedServer = { id: newServerId(), ...server };
    this.#set({ ...this.#state, servers: [...this.#state.servers, { saved, status: "disconnected" }] });
    return saved;
  }

  /** Records a sign-in to a saved server: it is connected from now on. */
  signedIn(id: string, registered: RegisteredRemote, auth: SignedIn): void {
    const signIn: RemoteSignIn = {
      serverId: id,
      relayId: registered.id,
      relayPath: registered.relay_path,
      token: auth.token,
      expiresAt: auth.expiresAt,
      username: auth.username,
      serverName: registered.server.name ?? "",
      version: registered.server.version ?? "",
      tls: registered.tls,
    };
    const saved = this.find(id)?.saved;
    this.#set({
      ...this.#state,
      relaySession: registered.relay_session || this.#state.relaySession,
      servers: this.#state.servers.map((s) =>
        s.saved.id === id
          ? {
              saved: {
                ...s.saved,
                ...(registered.tls?.fingerprint && saved?.trust ? { fingerprint: registered.tls.fingerprint } : {}),
              },
              status: "connected",
              signIn,
            }
          : s,
      ),
    });
  }

  /** Keeps the relay session the relay issued (registering without a sign-in anywhere). */
  relaySessionIssued(key: string): void {
    if (key && key !== this.#state.relaySession) this.#set({ ...this.#state, relaySession: key });
  }

  /** Forgets a sign-in (the server stays in the list); its connections are closed. */
  disconnect(id: string, why?: string): void {
    const state = this.find(id);
    // The sessions end first (through the relay), then the relay forgets the remote.
    const relayId = state?.signIn?.relayId;
    void this.#closeConnections(id).finally(() => {
      if (relayId) void this.api.remove(relayId).catch(() => {});
    });
    const { signIn: _gone, ...rest } = state ?? {
      saved: { id, name: "", host: "", port: defaultPort, tls: true, prefix: "", trust: false },
    };
    this.#set({
      ...this.#state,
      servers: this.#state.servers.map((s) =>
        s.saved.id === id ? { ...rest, status: why ? "error" : "disconnected", ...(why ? { error: why } : {}) } : s,
      ),
    });
  }

  /**
   * Removes a server from the list, disconnecting it first, forgetting a remembered password and
   * (in the desktop app) removing it from Settings > Remote servers. It stays removed: a desktop
   * hand-over doesn't bring it back.
   */
  remove(id: string): void {
    const saved = this.find(id)?.saved;
    if (saved) {
      const dismissed = loadDismissed(this.#user);
      dismissed.add(serverKey(saved));
      saveDismissed(this.#user, dismissed);
      void removeDesktopServer(saved).catch(() => {});
    }
    this.disconnect(id);
    void forgetRememberedPassword(id).catch(() => {});
    this.#set({ ...this.#state, servers: this.#state.servers.filter((s) => s.saved.id !== id) });
  }

  /** Renames a server (in the desktop app, in Settings > Remote servers too). */
  rename(id: string, name: string): void {
    const state = this.find(id);
    if (!state) return;
    this.#patch(id, { saved: { ...state.saved, name } });
    void renameDesktopServer(state.saved, name).catch(() => {});
  }

  /** The server's own session for listings (opened once); throws when it isn't connected. */
  connection(id: string): Promise<Connection> {
    const state = this.find(id);
    if (!state?.signIn) return Promise.reject(new RemoteError("Sign in to this server first.", "not_connected", 0));
    let conn = this.#connections.get(id);
    if (!conn) {
      conn = this.openConnection(id, { reconnect: false });
      conn.catch(() => this.#connections.delete(id));
      this.#connections.set(id, conn);
    }
    return conn;
  }

  /** Forgets the listings connection (after a lost session); the next call opens a new one. */
  forgetConnection(id: string): void {
    const conn = this.#connections.get(id);
    this.#connections.delete(id);
    void conn?.then((c) => c.close()).catch(() => {});
  }

  /** A new session on a connected server (an editor tab's, an upload's). */
  openConnection(id: string, options: ConnectionOptions = {}): Promise<Connection> {
    const state = this.find(id);
    if (!state?.signIn) return Promise.reject(new RemoteError("Sign in to this server first.", "not_connected", 0));
    return openRemoteConnection(state.signIn, () => this.relayAuth(), {
      ...options,
      timeoutS: state.saved.timeout ?? 0,
      resumeTimeoutS: state.saved.resumeTimeout,
      onResuming: (progress) => this.resuming(id, progress),
    }).then(
      (c) => {
        this.resuming(id, undefined);
        this.#extra.set(c, id);
        return c;
      },
      (err: unknown) => {
        this.resuming(id, undefined);
        throw err;
      },
    );
  }

  /** The open sessions on every server (to end them when the page goes). */
  openConnections(): { signIn: RemoteSignIn; connection: Connection }[] {
    const out: { signIn: RemoteSignIn; connection: Connection }[] = [];
    for (const [c, id] of this.#extra) {
      if (c.closed) {
        this.#extra.delete(c);
        continue;
      }
      const state = this.find(id);
      if (state?.signIn) out.push({ signIn: state.signIn, connection: c });
    }
    return out;
  }

  async #closeConnections(id: string): Promise<void> {
    this.forgetConnection(id);
    for (const [c, owner] of [...this.#extra]) {
      if (owner === id) {
        this.#extra.delete(c);
        await c.close().catch(() => {});
      }
    }
  }

  /** Closes every connection (signing out of the UI). */
  closeAll(): void {
    for (const id of [...this.#connections.keys()]) this.forgetConnection(id);
    for (const c of this.#extra.keys()) void c.close().catch(() => {});
    this.#extra.clear();
  }

  /**
   * Checks each saved sign-in against the relay after a reload: a remote the relay no longer
   * has (the server restarted) is registered again with the trusted fingerprint, keeping the
   * token; a token the remote refuses ends the sign-in.
   */
  async restore(signal?: AbortSignal): Promise<void> {
    for (const state of this.#state.servers) {
      if (!state.signIn) continue;
      const { signIn, saved } = state;
      try {
        await this.api.probe(
          {
            url: urlOf(saved),
            tls: tlsModeOf(saved),
            ...(saved.fingerprint ? { fingerprint: saved.fingerprint } : {}),
          },
          signal,
        );
        const listed = await this.api.list(signal);
        if (listed.remotes.some((r) => r.id === signIn.relayId)) continue;
        const registered = await this.api.register(
          {
            url: urlOf(saved),
            name: saved.name,
            tls: tlsModeOf(saved),
            ...(saved.fingerprint ? { fingerprint: saved.fingerprint } : {}),
          },
          signal,
        );
        this.signedIn(saved.id, registered, {
          username: signIn.username,
          token: signIn.token,
          expiresAt: signIn.expiresAt,
        });
      } catch (err) {
        if (signal?.aborted) return;
        this.disconnect(saved.id, err instanceof Error ? err.message : String(err));
      }
    }
  }
}

export function useServers(store: ServersStore): ServersSnapshot {
  return useSyncExternalStore(
    (l) => store.subscribe(l),
    () => store.snapshot(),
  );
}

/** A server's name for people: the saved name, else what it calls itself, else its URL. */
export function serverLabel(state: ServerState): string {
  return state.saved.name || state.signIn?.serverName || addressLabel(state.saved);
}

/** How a discovery document's server shows (the local server's label). */
export function discoveryLabel(doc: DiscoveryDocument | undefined): string {
  return `${doc?.server?.name ?? "Hardcharger"} ${doc?.server?.version ?? ""}`.trim();
}
