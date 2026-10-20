// Asking for the upload dialog from anywhere in the app (the query toolbar, the schema
// browser, a file dropped on the page): one request at a time, shown by UploadHost.

import { useSyncExternalStore } from "react";

export interface UploadRequest {
  /** Where the table goes (empty: the session's catalog or schema). */
  catalog?: string;
  schema?: string;
  /** The role the upload acts with (the active tab's; "" for all of the user's roles). */
  role?: string;
  /** A file dropped on the page. */
  file?: File;
}

let current: UploadRequest | null = null;
let serial = 0;
const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

/** Opens the upload dialog (or points the open one at a dropped file). */
export function requestUpload(request: UploadRequest): void {
  current = { ...request };
  serial++;
  emit();
}

export function closeUpload(): void {
  current = null;
  emit();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The open request, and a number that changes with each new one. */
export function useUploadRequest(): { request: UploadRequest | null; serial: number } {
  const request = useSyncExternalStore(subscribe, () => current);
  const n = useSyncExternalStore(subscribe, () => serial);
  return { request, serial: n };
}
