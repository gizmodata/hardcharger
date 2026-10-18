import { afterEach, describe, expect, it, vi } from "vitest";

import { SignInError, clearSignIn, lastUsername, loadSignIn, saveSignIn, signIn } from "../../src/lib/auth";

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
  localStorage.clear();
});

describe("sign-in storage", () => {
  it("keeps the token in sessionStorage only, and forgets an expired one", () => {
    saveSignIn({ username: "joe", token: "t0k3n", expiresAt: Date.now() + 60_000 });
    expect(loadSignIn()?.token).toBe("t0k3n");
    const local = Object.keys(localStorage).map((k) => localStorage.getItem(k));
    expect(JSON.stringify(local)).not.toContain("t0k3n");
    expect(loadSignIn(Date.now() + 120_000)).toBeNull();
    clearSignIn();
    expect(loadSignIn()).toBeNull();
    expect(lastUsername()).toBe("joe");
  });

  it("ignores what it can't read", () => {
    sessionStorage.setItem("hardcharger-ui:sign-in", "{not json");
    expect(loadSignIn()).toBeNull();
  });
});

describe("signIn", () => {
  it("sends the password grant as JSON and returns the token", async () => {
    const fetch = vi.fn(async () => Response.json({ access_token: "abc", token_type: "Bearer", expires_in: 60 }));
    vi.stubGlobal("fetch", fetch);
    const before = Date.now();
    const s = await signIn("joe", "p:a@ss");
    expect(s.token).toBe("abc");
    expect(s.expiresAt).toBeGreaterThanOrEqual(before + 60_000);
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/warp\/v1\/auth\/token$/);
    expect(JSON.parse(init.body as string)).toEqual({ grant_type: "password", username: "joe", password: "p:a@ss" });
    // Default credentials mode: under mutual TLS the browser presents its client certificate.
    expect(init.credentials).toBeUndefined();
  });

  it("reports the server's problem detail", async () => {
    vi.stubGlobal("fetch", async () =>
      Response.json(
        { code: "invalid_credentials", detail: "the user name or password is not correct", status: 401 },
        { status: 401 },
      ),
    );
    const err = await signIn("joe", "nope").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(SignInError);
    expect(err).toMatchObject({
      code: "invalid_credentials",
      status: 401,
      message: "the user name or password is not correct",
    });
  });

  it("says when the server can't be reached", async () => {
    vi.stubGlobal("fetch", async () => {
      throw new TypeError("Failed to fetch");
    });
    await expect(signIn("joe", "x")).rejects.toMatchObject({ code: "transport_error" });
  });
});

describe("signInWithCertificate", () => {
  it("asks for the client_certificate grant with no other credential", async () => {
    const fetch = vi.fn(async () => Response.json({ access_token: "bound", token_type: "Bearer", expires_in: 60 }));
    vi.stubGlobal("fetch", fetch);
    const { signInWithCertificate } = await import("../../src/lib/auth");
    expect((await signInWithCertificate()).token).toBe("bound");
    const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ grant_type: "client_certificate" });
  });
});
