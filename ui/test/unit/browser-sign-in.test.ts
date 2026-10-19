// Browser sign-in (Warp SQL §4.3) in the page: the flow's poll secret stays in this tab's
// sessionStorage, the return fragment is read once and removed, and only a flow this tab
// started is collected. Each test loads the module afresh (it reads the fragment once per load).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type Auth = typeof import("../../src/lib/auth");

async function freshAuth(): Promise<Auth> {
  vi.resetModules();
  return import("../../src/lib/auth");
}

beforeEach(() => {
  window.history.replaceState(null, "", "/ui/?tab=1");
});

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
  localStorage.clear();
});

const flow = {
  flow_id: "f_abc",
  poll_secret: "s3cret-never-in-a-url",
  authorize_url: "https://idp.example/authorize?state=xyz",
  poll_interval_s: 2,
  expires_at: "2026-10-19T21:00:00Z",
};

describe("startBrowserSignIn", () => {
  it("asks to come back to this page and keeps the poll secret in this tab only", async () => {
    const auth = await freshAuth();
    const fetch = vi.fn(async () => Response.json(flow, { status: 201 }));
    vi.stubGlobal("fetch", fetch);
    expect(await auth.startBrowserSignIn()).toBe(flow.authorize_url);
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/warp\/v1\/auth\/oauth\/flows$/);
    expect(JSON.parse(init.body as string)).toEqual({ return_to: "/ui/?tab=1" });
    expect(sessionStorage.getItem("hardcharger-ui:browser-sign-in")).toContain(flow.poll_secret);
    expect(JSON.stringify(Object.keys(localStorage).map((k) => localStorage.getItem(k)))).not.toContain(
      flow.poll_secret,
    );
  });

  it("reports the server's refusal", async () => {
    const auth = await freshAuth();
    vi.stubGlobal("fetch", async () =>
      Response.json({ code: "not_implemented", detail: "browser sign-in is not configured" }, { status: 501 }),
    );
    await expect(auth.startBrowserSignIn()).rejects.toMatchObject({ code: "not_implemented", status: 501 });
  });
});

describe("returning from the identity provider", () => {
  it("reads the flow id from the fragment once and removes it from the address bar", async () => {
    window.history.replaceState(null, "", "/ui/?tab=1#warp_oauth_flow=f_abc");
    const auth = await freshAuth();
    expect(auth.takeReturnedFlow()).toBe("f_abc");
    expect(window.location.hash).toBe("");
    expect(window.location.pathname + window.location.search).toBe("/ui/?tab=1");
    expect(auth.takeReturnedFlow()).toBe("f_abc"); // the same page load asks again
  });

  it("collects the token with the kept secret, once", async () => {
    window.history.replaceState(null, "", "/ui/#warp_oauth_flow=f_abc");
    const auth = await freshAuth();
    sessionStorage.setItem("hardcharger-ui:browser-sign-in", JSON.stringify({ flowId: "f_abc", pollSecret: "pss" }));
    const fetch = vi.fn(async () =>
      Response.json({ state: "complete", access_token: "tok", token_type: "Bearer", expires_in: 60 }),
    );
    vi.stubGlobal("fetch", fetch);
    const id = auth.takeReturnedFlow()!;
    const [a, b] = await Promise.all([auth.finishBrowserSignIn(id), auth.finishBrowserSignIn(id)]);
    expect(a?.token).toBe("tok");
    expect(b).toBe(a);
    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/warp\/v1\/auth\/oauth\/flows\/f_abc$/);
    expect(url).not.toContain("pss");
    expect(JSON.parse(init.body as string)).toEqual({ poll_secret: "pss" });
    expect(sessionStorage.getItem("hardcharger-ui:browser-sign-in")).toBeNull();
    // Collected: a later sign-in page (after signing out) doesn't sign in with it again.
    expect(auth.takeReturnedFlow()).toBeNull();
  });

  it("ignores a flow this tab didn't start", async () => {
    window.history.replaceState(null, "", "/ui/#warp_oauth_flow=f_someone_elses");
    const auth = await freshAuth();
    sessionStorage.setItem("hardcharger-ui:browser-sign-in", JSON.stringify({ flowId: "f_mine", pollSecret: "pss" }));
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    expect(await auth.finishBrowserSignIn(auth.takeReturnedFlow()!)).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
    expect(await (await freshAuth()).finishBrowserSignIn("f_none")).toBeNull();
  });

  it("says why a sign-in failed", async () => {
    const auth = await freshAuth();
    sessionStorage.setItem("hardcharger-ui:browser-sign-in", JSON.stringify({ flowId: "f_abc", pollSecret: "pss" }));
    vi.stubGlobal("fetch", async () =>
      Response.json({ state: "failed", error: { code: "access_denied", detail: "access_denied: The user declined" } }),
    );
    await expect(auth.finishBrowserSignIn("f_abc")).rejects.toMatchObject({
      code: "access_denied",
      message: "Sign-in failed: access_denied: The user declined",
    });
  });

  it("polls on through a flow state it doesn't know (Warp SQL §12)", async () => {
    vi.useFakeTimers();
    try {
      const auth = await freshAuth();
      sessionStorage.setItem("hardcharger-ui:browser-sign-in", JSON.stringify({ flowId: "f_abc", pollSecret: "pss" }));
      const answers = [{ state: "some_later_state" }, { state: "complete", access_token: "tok", expires_in: 60 }];
      vi.stubGlobal("fetch", async () => Response.json(answers.shift()));
      const signedIn = auth.finishBrowserSignIn("f_abc");
      await vi.runAllTimersAsync();
      await expect(signedIn).resolves.toMatchObject({ token: "tok" });
    } finally {
      vi.useRealTimers();
    }
  });

  it("says when the flow expired or was used", async () => {
    const auth = await freshAuth();
    sessionStorage.setItem("hardcharger-ui:browser-sign-in", JSON.stringify({ flowId: "f_abc", pollSecret: "pss" }));
    vi.stubGlobal("fetch", async () => Response.json({ code: "flow_not_found", detail: "no such" }, { status: 404 }));
    await expect(auth.finishBrowserSignIn("f_abc")).rejects.toMatchObject({ code: "flow_not_found" });
  });
});

describe("discovery", () => {
  it("offers browser sign-in only with the capability, with the provider's name", async () => {
    const auth = await freshAuth();
    expect(auth.offersBrowserSignIn({ capabilities: ["oauth_browser"] })).toBe(true);
    expect(auth.offersBrowserSignIn({ capabilities: ["ingest"] })).toBe(false);
    expect(auth.offersBrowserSignIn(undefined)).toBe(false);
    expect(auth.browserSignInProvider({ auth: { oauth_browser: { provider: "Okta" } } })).toBe("Okta");
    expect(auth.browserSignInProvider({ auth: { oauth_browser: {} } })).toBeUndefined();
  });
});
