// The Warp SQL connections the UI opens: always to its own origin, under /warp.

import { type Connection, type DiscoveryDocument, connect } from "@gizmodata/warp-sql";

import type { SignedIn } from "./auth";

/** The protocol's base URL: this page's origin, /warp. */
export function warpBase(): string {
  return `${window.location.origin}/warp`;
}

/** The server's discovery document (§3.1); needs no sign-in. */
export async function discover(signal?: AbortSignal): Promise<DiscoveryDocument> {
  const resp = await fetch(warpBase(), {
    headers: { Accept: "application/json" },
    cache: "no-store",
    signal: signal ?? null,
  });
  if (!resp.ok) throw new Error(`the server answered ${resp.status} at ${warpBase()}`);
  const doc = (await resp.json()) as DiscoveryDocument;
  if (doc.protocol !== "warp-sql") throw new Error(`${warpBase()} is not a Warp SQL server`);
  return doc;
}

/** A connection (one server session) signed in with the tab's access token. */
export function openConnection(auth: SignedIn, signal?: AbortSignal): Promise<Connection> {
  return connect({
    url: warpBase(),
    token: auth.token,
    // One stream per result: the UI reads at most the row limit, and the browser's six
    // connections per host are shared by every tab of the app.
    resultPartitions: 1,
    queryTag: { client: "hardcharger-ui" },
    logger: { warn: (message: string) => console.info(`[warp-sql] ${message}`) },
    ...(signal ? { signal } : {}),
  });
}
