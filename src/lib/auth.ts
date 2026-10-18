// Signing in with Hardcharger's own password flow (Warp SQL §4.1): the password goes to
// POST /warp/v1/auth/token once and is then forgotten. The browser keeps only the
// short-lived access token, in memory and in this tab's sessionStorage (so a reload stays
// signed in), never in localStorage or a cookie.

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
  let resp: Response;
  try {
    resp = await fetch(`${warpBase()}/v1/auth/token`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ grant_type: "password", username, password }),
      credentials: "omit",
      cache: "no-store",
      signal: signal ?? null,
    });
  } catch (err) {
    if (signal?.aborted) throw err;
    throw new SignInError("The server could not be reached.", "transport_error", undefined);
  }
  const body = (await resp.json().catch(() => ({}))) as Problem & {
    access_token?: string;
    expires_in?: number;
  };
  if (!resp.ok) {
    const detail = body.detail ?? body.title ?? `sign-in failed (HTTP ${resp.status})`;
    throw new SignInError(detail, body.code ?? "sign_in_failed", resp.status);
  }
  if (typeof body.access_token !== "string" || body.access_token === "") {
    throw new SignInError("The server answered without an access token.", "protocol_error", resp.status);
  }
  const lifetime = typeof body.expires_in === "number" && body.expires_in > 0 ? body.expires_in : 3600;
  return { username, token: body.access_token, expiresAt: Date.now() + lifetime * 1000 };
}
