// The catalog caches of a signed-in session: one per role the editor's tabs act with ("" for
// all of the user's roles, through the session's own connection), shared by the schema browser
// and autocomplete.

import type { Connection } from "@gizmodata/warp-sql";

import { CatalogCache } from "./catalogCache";

const bySession = new WeakMap<object, Map<string, CatalogCache>>();

/**
 * The cache for `role` in `session`. `open` gives the connection a role lists through (the
 * session's own connection for "").
 */
export function catalogCache(session: object, role: string, open: () => Promise<Connection>): CatalogCache {
  let caches = bySession.get(session);
  if (!caches) {
    caches = new Map();
    bySession.set(session, caches);
  }
  let cache = caches.get(role);
  if (!cache) {
    cache = new CatalogCache(open);
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
