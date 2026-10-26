// Where the query page's active tab stands, for the parts of the app outside the page (the
// schema browser): its catalog and schema, its transaction, and its role. The page publishes
// it; readers subscribe (useActiveTab).

import type { TransactionState } from "@gizmodata/warp-sql";
import { useSyncExternalStore } from "react";

export interface ActiveTab {
  catalog: string;
  schema: string;
  transaction: TransactionState;
  /** The role the tab acts with (SET ROLE); "" for all of the user's roles. */
  role: string;
  sessionId?: string;
  title?: string;
}

const none: ActiveTab = { catalog: "", schema: "", transaction: "none", role: "" };
let current: ActiveTab = none;
const listeners = new Set<() => void>();

export function publishActiveTab(next: ActiveTab): void {
  if (
    next.catalog === current.catalog &&
    next.schema === current.schema &&
    next.transaction === current.transaction &&
    next.role === current.role &&
    next.sessionId === current.sessionId &&
    next.title === current.title
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
