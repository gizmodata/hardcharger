// A server that is starting again after a stop (Chargewire §11.2, server_resuming): a
// GizmoData Cloud cluster with auto-resume has a holder at its address that answers 503
// server_resuming with a Retry-After while the cluster starts. The Chargewire client waits
// that out for its own requests; this does the same for the UI's own requests to a remote
// (its sign-in through the relay), with the same rules, and describes a wait for the page.

/** How long to wait for a resuming server unless a saved connection says otherwise (§11.2). */
export const defaultResumeTimeoutS = 600;
const defaultWaitMs = 3000; // no Retry-After, or one that isn't a whole number of seconds
const minWaitMs = 1000; // whatever it says: "0" must not repeat a request back to back
const maxWaitMs = 60_000;

/** Where a wait on a resuming server stands (chargewire-js's ResumingEvent, plus when it was told). */
export interface ResumeProgress {
  /** 1 for the first server_resuming answer, 2 for the next, ... */
  attempt: number;
  /** The wait before the request is sent again, from `at`. */
  retryAfterMs: number;
  /** Time since the first server_resuming answer, at `at`. */
  elapsedMs: number;
  resumeTimeoutMs: number;
  /** The server's detail, e.g. "the server is resuming; retry after 5 s". */
  detail: string;
  /** When this was told (Date.now()). */
  at: number;
}

/** The wait a server_resuming answer's Retry-After asks for, within the rules (§11.2). */
export function resumeWaitMs(retryAfter: string | null | undefined): number {
  const text = (retryAfter ?? "").trim();
  if (!/^\d{1,9}$/.test(text)) return defaultWaitMs;
  return Math.min(maxWaitMs, Math.max(minWaitMs, Number(text) * 1000));
}

/** The problem document's detail when `resp` is a server_resuming answer, else undefined. */
async function resumingDetail(resp: Response): Promise<string | undefined> {
  if (resp.status !== 503) return undefined;
  const body = (await resp
    .clone()
    .json()
    .catch(() => undefined)) as { code?: unknown; detail?: unknown } | undefined;
  if (body?.code !== "server_resuming") return undefined;
  return typeof body.detail === "string" && body.detail ? body.detail : "the server is resuming";
}

/** Why an aborted wait ends: the signal's reason when it is an error, else an AbortError. */
function abortError(signal: AbortSignal | undefined): Error {
  const reason: unknown = signal?.reason;
  return reason instanceof Error ? reason : new DOMException("Aborted", "AbortError");
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError(signal));
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(abortError(signal));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export interface ResumeOptions {
  /** Seconds to keep sending the request again; 0 reports the first answer. Default 600. */
  resumeTimeoutS?: number | undefined;
  /** Told before each wait. A throw is ignored. */
  onResuming?: ((progress: ResumeProgress) => void) | undefined;
  /** Stops a wait at once (the wait rejects with the signal's reason). */
  signal?: AbortSignal | undefined;
}

/**
 * Sends a request (`send` makes it anew each time: a body can only be sent once) and, while
 * the server answers server_resuming, waits as it asks and sends it again, until the resume
 * timeout would pass. Returns the first other answer, or the last server_resuming answer
 * when the time is up, for the caller to report.
 */
export async function sendWhileResuming(send: () => Promise<Response>, options: ResumeOptions = {}): Promise<Response> {
  const timeoutMs = Math.max(0, options.resumeTimeoutS ?? defaultResumeTimeoutS) * 1000;
  let first: number | undefined;
  let attempt = 0;
  for (;;) {
    const resp = await send();
    const detail = await resumingDetail(resp);
    if (detail === undefined) return resp;
    const now = Date.now();
    first ??= now;
    attempt += 1;
    const wait = resumeWaitMs(resp.headers.get("Retry-After"));
    if (timeoutMs === 0 || now - first + wait > timeoutMs) return resp;
    try {
      options.onResuming?.({
        attempt,
        retryAfterMs: wait,
        elapsedMs: now - first,
        resumeTimeoutMs: timeoutMs,
        detail,
        at: now,
      });
    } catch {
      // A display that failed must not change what the request does.
    }
    await sleep(wait, options.signal);
  }
}

/** "1:05": minutes and seconds. */
export function clock(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/**
 * The server's detail in parentheses, unless it only says what the sentence already does
 * ("the server is resuming; retry after 5 s", the spec's example): a holder's own words
 * ("cluster warehouse is starting, about 40 s left") are worth showing.
 */
function aside(detail: string): string {
  return /^the server is resuming\b[^a-z]*(retry after \d+ s)?\.?$/i.test(detail.trim()) ? "" : ` (${detail})`;
}

/** What a page shows at `now` for a wait: the countdown and the time spent so far. */
export function describeResume(p: ResumeProgress, now: number): { retryInS: number; sofar: string; text: string } {
  // A display's clock can lag a fresh report by up to its tick: never before the report.
  const t = Math.max(now, p.at);
  const retryInS = Math.max(0, Math.ceil((p.at + p.retryAfterMs - t) / 1000));
  const sofar = clock(p.elapsedMs + (t - p.at));
  return {
    retryInS,
    sofar,
    text: `The server is resuming${aside(p.detail)}: retrying in ${retryInS} s, ${sofar} so far.`,
  };
}
