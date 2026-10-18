// Recovery from a stale build. Every release replaces the hashed files of the lazily loaded
// pages, so a tab opened before an upgrade (or a page loaded while two replicas served
// different builds) can ask for a file that is gone. The page then reloads, to pick up the
// new index.html, after 0, 3 and 10 seconds (long enough for a rolling restart), and after
// that says that a new version is available instead of failing silently.

export const reloadDelaysMs = [0, 3000, 10_000] as const;

const key = "hardcharger-ui:stale-reloads";
// Attempts older than this belong to an earlier episode and don't count.
const episodeMs = 5 * 60_000;

/** A lazily loaded file failed to load: the build changed under the page. */
export class StaleBuildError extends Error {
  override readonly name = "StaleBuildError";
  constructor(cause: unknown) {
    super("this page's files have changed on the server", { cause });
  }
}

/** Whether an error is a failed fetch of a module or a preloaded file. */
export function isChunkLoadError(err: unknown): boolean {
  if (err instanceof StaleBuildError) return true;
  const message = err instanceof Error ? err.message : String(err);
  return [
    "Failed to fetch dynamically imported module", // Chromium
    "Importing a module script failed", // WebKit
    "error loading dynamically imported module", // Firefox
    "Unable to preload CSS", // Vite's CSS preload
    "Failed to load module script",
  ].some((m) => message.includes(m));
}

/**
 * Wraps a dynamic import so a missing file becomes a StaleBuildError. When the module loads
 * after a failure of its own, earlier reload attempts are forgotten.
 */
export function loadModule<T>(name: string, load: () => Promise<T>): () => Promise<T> {
  return () =>
    load().then(
      (module) => {
        if (failedModule() === name) recovered();
        return module;
      },
      (err: unknown) => {
        if (!isChunkLoadError(err)) throw err;
        try {
          session()?.setItem(moduleKey, name);
        } catch {
          // no storage: nothing to remember
        }
        throw new StaleBuildError(err);
      },
    );
}

const moduleKey = "hardcharger-ui:stale-module";

function failedModule(): string | null {
  try {
    return session()?.getItem(moduleKey) ?? null;
  } catch {
    return null;
  }
}

interface Attempts {
  count: number;
  at: number;
}

function read(storage: Storage | undefined, now: number): Attempts {
  try {
    const raw = storage?.getItem(key);
    if (!raw) return { count: 0, at: 0 };
    const parsed = JSON.parse(raw) as Attempts;
    if (typeof parsed.count !== "number" || now - parsed.at > episodeMs) return { count: 0, at: 0 };
    return parsed;
  } catch {
    return { count: 0, at: 0 };
  }
}

function session(): Storage | undefined {
  try {
    return window.sessionStorage;
  } catch {
    return undefined;
  }
}

export type Recovery = { kind: "reloading"; attempt: number; delayMs: number } | { kind: "gave-up" };

/**
 * Plans the next step after a stale-build failure: a reload after the next delay, or
 * giving up. `reload` is called when the delay has passed (window.location.reload).
 */
export function recover(
  reload: () => void = () => window.location.reload(),
  options: { storage?: Storage | undefined; now?: number; schedule?: (fn: () => void, ms: number) => void } = {},
): Recovery {
  const storage = "storage" in options ? options.storage : session();
  const now = options.now ?? Date.now();
  const schedule = options.schedule ?? ((fn, ms) => window.setTimeout(fn, ms));
  const attempts = read(storage, now);
  if (attempts.count >= reloadDelaysMs.length) return { kind: "gave-up" };
  const delayMs = reloadDelaysMs[attempts.count]!;
  try {
    storage?.setItem(key, JSON.stringify({ count: attempts.count + 1, at: now }));
  } catch {
    // Without storage the page can't count its reloads: reload once, then it gives up.
    if (attempts.count > 0) return { kind: "gave-up" };
  }
  schedule(reload, delayMs);
  return { kind: "reloading", attempt: attempts.count + 1, delayMs };
}

/** Forgets earlier attempts: the lazily loaded files load again. */
export function recovered(storage: Storage | undefined = session()): void {
  try {
    storage?.removeItem(key);
    storage?.removeItem(moduleKey);
  } catch {
    // nothing to forget
  }
}
