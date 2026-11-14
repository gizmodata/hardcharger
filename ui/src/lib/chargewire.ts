// The Chargewire connections the UI opens: always to its own origin, under /chargewire.

import { type Connection, type DiscoveryDocument, connect } from "@gizmodata/chargewire";

import type { SignedIn } from "./auth";
import { pageRequests } from "./pageRequests";

/** The protocol's base URL: this page's origin, /chargewire. */
export function chargewireBase(): string {
  return `${window.location.origin}/chargewire`;
}

/** The server's discovery document (§3.1); needs no sign-in. */
export async function discover(signal?: AbortSignal): Promise<DiscoveryDocument> {
  const resp = await pageRequests.fetch(chargewireBase(), {
    headers: { Accept: "application/json" },
    cache: "no-store",
    signal: signal ?? null,
  });
  if (!resp.ok) throw new Error(`the server answered ${resp.status} at ${chargewireBase()}`);
  const doc = (await resp.json()) as DiscoveryDocument;
  if (doc.protocol !== "chargewire") throw new Error(`${chargewireBase()} is not a Chargewire server`);
  return doc;
}

export interface ConnectionOptions {
  signal?: AbortSignal;
  /** The session's catalog and schema (empty: the user's default). */
  catalog?: string;
  schema?: string;
  /** Request server-enforced read-only mode when creating this session. */
  readOnly?: boolean;
  /**
   * false: a session the server lost fails with session_not_found instead of being replaced
   * (an editor tab decides itself, since it knows whether a transaction was open).
   */
  reconnect?: boolean;
  /** null leaves tags to the server session (for user SQL in the editor). */
  queryTag?: Record<string, unknown> | null;
}

/** A connection (one server session) signed in with the tab's access token. */
export async function openConnection(auth: SignedIn, options: ConnectionOptions = {}): Promise<Connection> {
  const { signal, catalog, schema, reconnect } = options;
  const conn = await connect({
    url: chargewireBase(),
    fetch: pageRequests.fetch,
    token: auth.token,
    ...(catalog ? { catalog } : {}),
    ...(schema ? { schema } : {}),
    ...(reconnect === undefined ? {} : { reconnect }),
    ...(options.readOnly ? { sessionOptions: { read_only: true }, reconnect: false } : {}),
    // One stream per result: the UI reads at most the row limit, and the browser's six
    // connections per host are shared by every tab of the app.
    resultPartitions: 1,
    ...(options.queryTag === null ? {} : { queryTag: { client: "hardcharger-ui", ...options.queryTag } }),
    logger: { warn: (message: string) => console.info(`[chargewire] ${message}`) },
    ...(signal ? { signal } : {}),
  });
  return confirmReadOnly(conn, options.readOnly);
}

/** Never expose a requested read-only connection before the server confirms it. */
export async function confirmReadOnly(conn: Connection, requested = false): Promise<Connection> {
  if (!requested) return conn;
  try {
    if ((await conn.getSession()).options?.["read_only"] !== true) {
      throw new Error("The server did not confirm read-only mode. The session was closed.");
    }
    return conn;
  } catch (err) {
    await conn.close().catch(() => {});
    throw err;
  }
}
