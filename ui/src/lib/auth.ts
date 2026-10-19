// Signing in with Hardcharger's own flows (Warp SQL §4.1, §4.4): a user name and password,
// sent to POST /warp/v1/auth/token once and then forgotten, or the client certificate the
// browser presents (the client_certificate grant, on a server with mutual TLS that offers
// it). The browser keeps only the short-lived access token, in memory and in this tab's
// sessionStorage (so a reload stays signed in), never in localStorage or a cookie.
//
// Requests use fetch's default credentials mode (same-origin), never "omit": under mutual
// TLS the browser must present its client certificate on every connection to the server,
// and an uncredentialed request goes without it. The server reads no cookies (§4.2).

import { warpBase } from "./warp";

export interface SignedIn {
  username: string;
  token: string;
  /** Milliseconds since the epoch. */
  expiresAt: number;
}

const key = "hardcharger-ui:sign-in";
const userKey = "hardcharger-ui:user-name";

export class SignInError extends Error {
  override readonly name = "SignInError";
  constructor(
    message: string,
    readonly code: string,
    readonly status: number | undefined,
  ) {
    super(message);
  }
}

function storage(): Storage | undefined {
  try {
    return window.sessionStorage;
  } catch {
    return undefined;
  }
}

/** The sign-in this tab saved, unless it has expired (or is about to). */
export function loadSignIn(now = Date.now()): SignedIn | null {
  try {
    const raw = storage()?.getItem(key);
    if (!raw) return null;
    const s = JSON.parse(raw) as Partial<SignedIn>;
    if (typeof s.token !== "string" || typeof s.username !== "string" || typeof s.expiresAt !== "number") return null;
    if (s.expiresAt - now < 5000) return null;
    return { username: s.username, token: s.token, expiresAt: s.expiresAt };
  } catch {
    return null;
  }
}

export function saveSignIn(s: SignedIn): void {
  try {
    storage()?.setItem(key, JSON.stringify(s));
    storage()?.setItem(userKey, s.username);
  } catch {
    // Private mode without storage: the sign-in lasts until the page reloads.
  }
}

export function clearSignIn(): void {
  try {
    storage()?.removeItem(key);
  } catch {
    // nothing saved
  }
}

/** The user name of the last sign-in in this tab, to fill in the form again. */
export function lastUsername(): string {
  try {
    return storage()?.getItem(userKey) ?? "";
  } catch {
    return "";
  }
}

interface Problem {
  code?: string;
  detail?: string;
  title?: string;
}

/** Exchanges a user name and password for an access token. */
export async function signIn(username: string, password: string, signal?: AbortSignal): Promise<SignedIn> {
  return requestToken({ grant_type: "password", username, password }, username, signal);
}

/** Signs in with the client certificate this connection presented (§4.4). */
export async function signInWithCertificate(signal?: AbortSignal): Promise<SignedIn> {
  return requestToken({ grant_type: "client_certificate" }, "", signal);
}

async function requestToken(grant: Record<string, string>, username: string, signal?: AbortSignal): Promise<SignedIn> {
  let resp: Response;
  try {
    resp = await fetch(`${warpBase()}/v1/auth/token`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(grant),
      cache: "no-store",
      signal: signal ?? null,
    });
  } catch (err) {
    if (signal?.aborted) throw err;
    throw new SignInError("The server could not be reached.", "client_transport_error", undefined);
  }
  const body = (await resp.json().catch(() => ({}))) as Problem & {
    access_token?: string;
    expires_in?: number;
  };
  if (!resp.ok) {
    const detail = body.detail ?? body.title ?? `sign-in failed (HTTP ${resp.status})`;
    throw new SignInError(detail, body.code ?? "client_sign_in_failed", resp.status);
  }
  if (typeof body.access_token !== "string" || body.access_token === "") {
    throw new SignInError("The server answered without an access token.", "client_protocol_error", resp.status);
  }
  const lifetime = typeof body.expires_in === "number" && body.expires_in > 0 ? body.expires_in : 3600;
  return { username, token: body.access_token, expiresAt: Date.now() + lifetime * 1000 };
}

// ---- Browser sign-in (Warp SQL §4.3): the server's identity provider ----
//
// The page creates a flow with return_to set to itself, keeps the flow's poll secret in this
// tab's sessionStorage (never in a URL, localStorage or a cookie), and sends the browser to
// the identity provider. The server's callback sends it back here with the flow id in the
// URL fragment (#warp_oauth_flow=…); the page removes the fragment at once and polls the flow
// with the secret it kept, which hands out the token once. A fragment naming a flow this tab
// didn't start is ignored.

const flowKey = "hardcharger-ui:browser-sign-in";
const fragmentKey = "warp_oauth_flow";

interface StoredFlow {
  flowId: string;
  pollSecret: string;
}

/** The identity provider's name the server gives (`auth.oauth_browser.provider`), if any. */
export function browserSignInProvider(server: { auth?: unknown } | undefined): string | undefined {
  const auth = server?.auth as { oauth_browser?: { provider?: unknown } } | undefined;
  const name = auth?.oauth_browser?.provider;
  return typeof name === "string" && name !== "" ? name : undefined;
}

/** Whether the server offers browser sign-in (capability oauth_browser). */
export function offersBrowserSignIn(server: { capabilities?: readonly string[] } | undefined): boolean {
  return server?.capabilities?.includes("oauth_browser") ?? false;
}

/**
 * Starts a browser sign-in and returns the URL to send the browser to. The page comes back
 * to where it is now (its path and query; the fragment is never sent).
 */
export async function startBrowserSignIn(signal?: AbortSignal): Promise<string> {
  const returnTo = window.location.pathname + window.location.search;
  let resp: Response;
  try {
    resp = await fetch(`${warpBase()}/v1/auth/oauth/flows`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ return_to: returnTo }),
      cache: "no-store",
      signal: signal ?? null,
    });
  } catch (err) {
    if (signal?.aborted) throw err;
    throw new SignInError("The server could not be reached.", "transport_error", undefined);
  }
  const body = (await resp.json().catch(() => ({}))) as Problem & {
    flow_id?: string;
    poll_secret?: string;
    authorize_url?: string;
  };
  if (!resp.ok) {
    const detail = body.detail ?? body.title ?? `the sign-in could not start (HTTP ${resp.status})`;
    throw new SignInError(detail, body.code ?? "sign_in_failed", resp.status);
  }
  if (!body.flow_id || !body.poll_secret || !body.authorize_url) {
    throw new SignInError("The server answered without a sign-in to follow.", "protocol_error", resp.status);
  }
  const flow: StoredFlow = { flowId: body.flow_id, pollSecret: body.poll_secret };
  try {
    storage()?.setItem(flowKey, JSON.stringify(flow));
  } catch {
    // Without sessionStorage the page can't collect the token when it comes back.
    throw new SignInError("This browser keeps no data for this tab, which sign-in needs.", "no_storage", undefined);
  }
  return body.authorize_url;
}

/**
 * The flow id this page was sent back with (the #warp_oauth_flow= fragment), removed from
 * the address bar; null when there is none, or once that flow has been collected. The
 * fragment is read once per page load (React may ask twice).
 */
export function takeReturnedFlow(): string | null {
  if (returnedFlow === undefined) returnedFlow = readReturnedFlow();
  return returnedFlow !== null && collectedFlows.has(returnedFlow) ? null : returnedFlow;
}

let returnedFlow: string | null | undefined;
const collectedFlows = new Set<string>();

function readReturnedFlow(): string | null {
  const hash = window.location.hash.replace(/^#/, "");
  if (!hash) return null;
  const params = new URLSearchParams(hash);
  const flowId = params.get(fragmentKey);
  if (flowId === null) return null;
  params.delete(fragmentKey);
  const rest = params.toString();
  window.history.replaceState(
    window.history.state,
    "",
    window.location.pathname + window.location.search + (rest ? `#${rest}` : ""),
  );
  return flowId;
}

/**
 * Collects the token of the flow this tab started and the server sent it back from. Null
 * when this tab didn't start `flowId` (someone else's link): the page ignores it.
 */
export function finishBrowserSignIn(flowId: string): Promise<SignedIn | null> {
  // Once per flow, however often it is asked (React runs a mount's effect twice in development).
  let finishing = finishingFlows.get(flowId);
  if (!finishing) {
    finishing = collectFlow(flowId).finally(() => {
      collectedFlows.add(flowId);
      finishingFlows.delete(flowId);
    });
    finishingFlows.set(flowId, finishing);
  }
  return finishing;
}

const finishingFlows = new Map<string, Promise<SignedIn | null>>();

async function collectFlow(flowId: string): Promise<SignedIn | null> {
  let flow: StoredFlow | null;
  try {
    const raw = storage()?.getItem(flowKey);
    flow = raw ? (JSON.parse(raw) as StoredFlow) : null;
  } catch {
    flow = null;
  }
  if (!flow || flow.flowId !== flowId || typeof flow.pollSecret !== "string") return null;
  try {
    storage()?.removeItem(flowKey);
  } catch {
    // nothing saved
  }
  // The callback finished the sign-in before it sent the browser here, so the first poll
  // has the outcome; a few more cover a server that answers it later.
  for (let attempt = 0; attempt < 10; attempt++) {
    let resp: Response;
    try {
      resp = await fetch(`${warpBase()}/v1/auth/oauth/flows/${encodeURIComponent(flowId)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ poll_secret: flow.pollSecret }),
        cache: "no-store",
      });
    } catch {
      throw new SignInError("The server could not be reached.", "transport_error", undefined);
    }
    const body = (await resp.json().catch(() => ({}))) as Problem & {
      state?: string;
      access_token?: string;
      expires_in?: number;
      error?: { code?: string; detail?: string };
    };
    if (!resp.ok) {
      if (body.code === "flow_not_found") {
        throw new SignInError("The sign-in expired or was already used. Sign in again.", body.code, resp.status);
      }
      throw new SignInError(
        body.detail ?? `sign-in failed (HTTP ${resp.status})`,
        body.code ?? "sign_in_failed",
        resp.status,
      );
    }
    if (body.state === "complete" && typeof body.access_token === "string" && body.access_token !== "") {
      const lifetime = typeof body.expires_in === "number" && body.expires_in > 0 ? body.expires_in : 3600;
      return { username: "", token: body.access_token, expiresAt: Date.now() + lifetime * 1000 };
    }
    if (body.state === "failed") {
      const code = body.error?.code ?? "sign_in_failed";
      const detail = body.error?.detail ?? "the identity provider did not sign you in";
      throw new SignInError(`Sign-in failed: ${detail}`, code, resp.status);
    }
    if (body.state !== "pending") {
      throw new SignInError(
        "The server's answer to the sign-in is not one this page knows.",
        "protocol_error",
        resp.status,
      );
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new SignInError("The sign-in did not finish. Sign in again.", "timeout", undefined);
}
