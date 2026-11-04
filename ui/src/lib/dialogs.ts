// The page's own confirm, prompt and notice dialogs. The browser's window.confirm, prompt and
// alert don't exist in the desktop app's webview (WKWebView answers confirm with false at once,
// so every "Remove?" did nothing), and they block the page, so the app never uses them: these
// open an in-page dialog (DialogHost, mounted once at the app's root) and resolve with the
// answer. ESLint's no-alert rule keeps the native ones out, and the e2e suite makes them throw.

import { useSyncExternalStore } from "react";

export interface ConfirmOptions {
  title?: string;
  /** The confirming button's label (default OK). */
  okLabel?: string;
  /** A destructive action: the button shows it. */
  danger?: boolean;
}

export interface PromptOptions {
  title?: string;
  okLabel?: string;
}

export type DialogRequest =
  | { id: number; kind: "confirm"; message: string; options: ConfirmOptions; resolve: (ok: boolean) => void }
  | {
      id: number;
      kind: "prompt";
      label: string;
      initial: string;
      options: PromptOptions;
      resolve: (value: string | undefined) => void;
    }
  | { id: number; kind: "notify"; message: string; title?: string; resolve: () => void };

let queue: DialogRequest[] = [];
let next = 1;
const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

function push(request: DialogRequest): void {
  queue = [...queue, request];
  emit();
}

/** Asks a yes/no question in the page; resolves true for OK, false for Cancel or Esc. */
export function confirmDialog(message: string, options: ConfirmOptions = {}): Promise<boolean> {
  return new Promise((resolve) => push({ id: next++, kind: "confirm", message, options, resolve }));
}

/** Asks for a line of text; resolves with it, or undefined for Cancel or Esc. */
export function promptDialog(label: string, initial = "", options: PromptOptions = {}): Promise<string | undefined> {
  return new Promise((resolve) => push({ id: next++, kind: "prompt", label, initial, options, resolve }));
}

/** Tells the user something; resolves when they close it. */
export function notifyDialog(message: string, title?: string): Promise<void> {
  return new Promise((resolve) => push({ id: next++, kind: "notify", message, ...(title ? { title } : {}), resolve }));
}

/** Answers the dialog on show and moves on to the next one (DialogHost calls it). */
export function settleDialog(id: number, answer: boolean | string | undefined): void {
  const request = queue.find((r) => r.id === id);
  if (!request) return;
  queue = queue.filter((r) => r.id !== id);
  emit();
  if (request.kind === "confirm") request.resolve(answer === true);
  else if (request.kind === "prompt") request.resolve(typeof answer === "string" ? answer : undefined);
  else request.resolve();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The dialog to show now (the oldest open request), if any. */
export function useCurrentDialog(): DialogRequest | undefined {
  return useSyncExternalStore(subscribe, () => queue[0]);
}
