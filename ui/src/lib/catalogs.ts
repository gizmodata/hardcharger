// The catalog caches of a signed-in session: one per server and role the editor's tabs act
// with ("" for this server, and "" for all of the user's roles, through the session's own
// connection), shared by the schema browser and autocomplete.

import type { Connection } from "@gizmodata/chargewire";

import { CatalogCache, type ConnectionSource } from "./catalogCache";
import { RoleConnection } from "./roles";
import type { ConnectionOptions } from "./chargewire";
import { localServerId } from "./servers";

const bySession = new WeakMap<object, Map<string, CatalogCache>>();

/**
 * The cache for `role` in `session`. `source` gives the connection a role lists through (the
 * session's own connection for ""); it is asked for once per role.
 */
export function catalogCache(session: object, role: string, source: () => ConnectionSource): CatalogCache {
  let caches = bySession.get(session);
  if (!caches) {
    caches = new Map();
    bySession.set(session, caches);
  }
  let cache = caches.get(role);
  if (!cache) {
    cache = new CatalogCache(source());
    caches.set(role, cache);
  }
  return cache;
}

const listeners = new Set<() => void>();

/** Forgets every role's listings in `session` (after a run that may have changed the catalog). */
export function clearCatalogCaches(session: object): void {
  for (const cache of bySession.get(session)?.values() ?? []) cache.clear();
  for (const listener of listeners) listener();
}

/** Calls `listener` after the listings were cleared (to list what's open again). */
export function onCatalogsChanged(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

interface Signed {
  connection: Connection;
  openConnection: (options?: ConnectionOptions & { server?: string }) => Promise<Connection>;
  servers: { connection: (id: string) => Promise<Connection>; forgetConnection: (id: string) => void };
}

/**
 * The listings `role` sees in a signed-in session on `server` ("" for this server): through
 * the session's own connection for "" (all of the user's roles), else through a connection
 * of the role's own (SET ROLE). On another server, through that server's own listing
 * session (roles are this server's).
 */
export function listingsFor(app: Signed, role: string, server: string = localServerId): CatalogCache {
  const key = server ? `${server}\u0000${role}` : role;
  return catalogCache(app, key, () => {
    if (server) {
      return {
        run: async (work) => {
          try {
            return await work(await app.servers.connection(server));
          } catch (err) {
            // A listing session the remote lost (idle, a restart): the next call opens a new one.
            if (sessionGone(err)) app.servers.forgetConnection(server);
            throw err;
          }
        },
      };
    }
    if (!role) return { run: (work) => work(app.connection) };
    const conn = new RoleConnection(role, () => app.openConnection({ reconnect: false }));
    return { run: (work) => conn.run(work) };
  });
}

function sessionGone(err: unknown): boolean {
  return typeof err === "object" && err !== null && (err as { code?: unknown }).code === "session_not_found";
}
