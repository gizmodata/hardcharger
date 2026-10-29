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
  /**
   * false: a session the server lost fails with session_not_found instead of being replaced
   * (an editor tab decides itself, since it knows whether a transaction was open).
   */
  reconnect?: boolean;
  queryTag?: Record<string, string>;
}

/** A connection (one server session) signed in with the tab's access token. */
export function openConnection(auth: SignedIn, options: ConnectionOptions = {}): Promise<Connection> {
  const { signal, catalog, schema, reconnect } = options;
  return connect({
    url: chargewireBase(),
    fetch: pageRequests.fetch,
    token: auth.token,
    ...(catalog ? { catalog } : {}),
    ...(schema ? { schema } : {}),
    ...(reconnect === undefined ? {} : { reconnect }),
    // One stream per result: the UI reads at most the row limit, and the browser's six
    // connections per host are shared by every tab of the app.
    resultPartitions: 1,
    queryTag: { client: "hardcharger-ui", ...options.queryTag },
    logger: { warn: (message: string) => console.info(`[chargewire] ${message}`) },
    ...(signal ? { signal } : {}),
  });
}
