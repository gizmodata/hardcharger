// System Admin (Hardcharger 2.14): a user granted SYSTEM ADMIN acts as the administrator in a
// session that says SET ROLE SYSTEM ADMIN, until RESET ROLE, a revoke, or the session's end.
// The server asks the grants (and the access control store) before every statement of such a
// session; the UI never decides it. What the UI knows is what each editor tab's session last
// answered when asked HARDCHARGER_IS_SYSTEM_ADMIN(), and it asks again whenever that may have
// changed: after a SET, RESET or EXECUTE in the tab, after a statement that failed while the tab
// acted as System Admin (a revoke shows up as a refused statement), after the tab's role or
// read-only mode changed, and when an admin page's own session was refused as System Admin.
// While the session is refused as System Admin (it asked and does not act as one, which the
// server may undo until RESET ROLE), it is asked at every read. A statement that succeeded
// needs no question: a session that asked and does not act as one runs nothing.
//
// While a tab on this server acts as System Admin, the admin pages are offered, and each opens
// its own session that says SET ROLE SYSTEM ADMIN too (and confirms it), so what they show and
// do is the server's answer to a session acting as System Admin. When no tab does, they close
// that session (which ends its elevation on the server) and are offered no more.

import { type Connection, ChargewireError, ErrorCode } from "@gizmodata/chargewire";
import { useSyncExternalStore } from "react";

import { firstKeyword } from "./sql";
import { internalQueryTag } from "./queryTags";

/** The statement that makes a session act as System Admin (two words, never quoted). */
export const elevateSql = "SET ROLE SYSTEM ADMIN";

/** Whether the session acts as System Admin right now, in the server's own words. */
export const systemAdminCheckSql = "SELECT HARDCHARGER_IS_SYSTEM_ADMIN() AS system_admin";

/**
 * Asks the session itself. Anything but a plain `true` is "no": a session that said SET ROLE
 * SYSTEM ADMIN and lost it (revoked, or the store did not answer) runs nothing, this included.
 */
export async function actsAsSystemAdmin(conn: Connection): Promise<boolean> {
  const table = await conn.query(systemAdminCheckSql, undefined, { queryTag: internalQueryTag });
  const row: unknown = table.get(0)?.toJSON();
  return (row as { system_admin?: unknown } | undefined)?.system_admin === true;
}

/**
 * A statement after which what the session acts as may have changed: SET ROLE, RESET ROLE (or
 * RESET ALL), and EXECUTE of a prepared one. Judged by the first keyword only, so it errs
 * towards asking again.
 */
export function mayChangeRole(sql: string): boolean {
  const word = firstKeyword(sql);
  return word === "SET" || word === "RESET" || word === "EXECUTE";
}

/**
 * The server's refusal of a statement because the session asked for SYSTEM ADMIN and does not
 * act as one (its user no longer holds it, or the store did not answer), or of SET ROLE SYSTEM
 * ADMIN itself.
 */
export function isSystemAdminRefusal(err: unknown): boolean {
  if (!ChargewireError.is(err) || err.code !== ErrorCode.permissionDenied) return false;
  const text = `${err.detail ?? ""} ${err.message}`;
  return /SYSTEM ADMIN|System Admin/.test(text);
}

interface TabEntry {
  elevated: boolean;
  /** Asks the tab's session again (now if the tab is idle, else when its run ends). */
  recheck: () => void;
}

/**
 * The editor tabs on this page whose sessions on this server act as System Admin, as each
 * session last answered. Tabs report themselves; the header and the admin pages read it.
 */
export class SystemAdminTabs {
  readonly #tabs = new Map<string, TabEntry>();
  readonly #listeners = new Set<() => void>();
  #elevated = false;

  /** A tab's session answered (`elevated`), or the tab has no session (false). */
  report(tab: string, elevated: boolean, recheck: () => void): void {
    this.#tabs.set(tab, { elevated, recheck });
    this.#changed();
  }

  /** The tab closed, or moved to another server. */
  forget(tab: string): void {
    if (this.#tabs.delete(tab)) this.#changed();
  }

  /** True while at least one tab's session last said it acts as System Admin. */
  get elevated(): boolean {
    return this.#elevated;
  }

  /** The ids of those tabs. */
  get tabs(): string[] {
    return [...this.#tabs].filter(([, t]) => t.elevated).map(([id]) => id);
  }

  /**
   * Something was refused as System Admin (an admin page's own session): every tab that
   * acted as one asks its session again. Until a tab's session says yes again it counts as
   * no, so the admin pages close at once.
   */
  recheck(): void {
    const elevated = [...this.#tabs.values()].filter((t) => t.elevated);
    for (const t of elevated) t.elevated = false;
    if (elevated.length) this.#changed();
    for (const t of elevated) t.recheck();
  }

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  #changed(): void {
    const now = [...this.#tabs.values()].some((t) => t.elevated);
    if (now === this.#elevated) return;
    this.#elevated = now;
    for (const listener of [...this.#listeners]) listener();
  }
}

/** Whether a tab on this server acts as System Admin (re-renders when that changes). */
export function useSystemAdminTabs(tabs: SystemAdminTabs | undefined): boolean {
  return useSyncExternalStore(
    tabs ? tabs.subscribe : noSubscription,
    () => tabs?.elevated ?? false,
    () => false,
  );
}

const noSubscription = () => () => {};

/**
 * An admin page's connection that reports refusals as System Admin (so every tab asks its
 * session again). Methods are called on the connection itself.
 */
export function reportingRefusals(conn: Connection, onRefusal: () => void): Connection {
  return new Proxy(conn, {
    get(target, prop) {
      const value: unknown = Reflect.get(target, prop, target);
      if (typeof value !== "function") return value;
      return (...args: unknown[]) => {
        const result: unknown = (value as (...a: unknown[]) => unknown).apply(target, args);
        if (result instanceof Promise) {
          return result.catch((err: unknown) => {
            if (isSystemAdminRefusal(err)) onRefusal();
            throw err;
          });
        }
        return result;
      };
    },
  });
}
