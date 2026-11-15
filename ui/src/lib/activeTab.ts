// Where the query page's active tab stands, for the parts of the app outside the page (the
// schema browser): its catalog and schema, its transaction, and its role. The page publishes
// it; readers subscribe (useActiveTab).

import type { TransactionState } from "@gizmodata/chargewire";
import { useSyncExternalStore } from "react";

export interface ActiveTab {
  catalog: string;
  schema: string;
  transaction: TransactionState;
  /** The role the tab acts with (SET ROLE); "" for all of the user's roles. */
  role: string;
  /** The server the tab is on (a saved server's id; "" for this server). */
  server: string;
  sessionId?: string;
  title?: string;
  /** The tab's Read-only box is checked or awaiting the server (a dropped file's upload follows it). */
  readOnly?: boolean;
}

const none: ActiveTab = { catalog: "", schema: "", transaction: "none", role: "", server: "" };
let current: ActiveTab = none;
const listeners = new Set<() => void>();

export function publishActiveTab(next: ActiveTab): void {
  if (
    next.catalog === current.catalog &&
    next.schema === current.schema &&
    next.transaction === current.transaction &&
    next.role === current.role &&
    next.server === current.server &&
    next.sessionId === current.sessionId &&
    next.title === current.title &&
    next.readOnly === current.readOnly
  ) {
    return;
  }
  current = next;
  for (const l of listeners) l();
}

/** Where the active tab stands now (for callbacks; components use useActiveTab). */
export function currentActiveTab(): ActiveTab {
  return current;
}

export function resetActiveTab(): void {
  publishActiveTab(none);
}

/** Calls `listener` whenever the active tab changes. */
export function subscribeActiveTab(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useActiveTab(): ActiveTab {
  return useSyncExternalStore(subscribeActiveTab, () => current);
}
