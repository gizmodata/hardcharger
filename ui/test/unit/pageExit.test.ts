import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { endSessionsOnExit, requestWindowClose, warnBeforeLeaving } from "../../src/lib/pageExit";

function pageEvent(type: string, persisted = false): Event {
  const e = new Event(type, { cancelable: true });
  Object.defineProperty(e, "persisted", { value: persisted });
  return e;
}

describe("endSessionsOnExit", () => {
  const fetches: { url: string; init: RequestInit }[] = [];
  let stop: () => void = () => {};
  let ids: string[] = [];
  const reload = vi.fn();

  beforeEach(() => {
    fetches.length = 0;
    ids = ["s_one", "s/two"];
    reload.mockReset();
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init: RequestInit) => {
        fetches.push({ url, init });
        return Promise.resolve(new Response(null, { status: 204 }));
      }),
    );
    stop = endSessionsOnExit(() => ({ token: "tok", ids }), reload);
  });
  afterEach(() => {
    stop();
    vi.unstubAllGlobals();
  });

  it("ends every open session with a keepalive DELETE carrying the token, once", () => {
    window.dispatchEvent(pageEvent("pagehide"));
    expect(fetches.map((f) => f.url)).toEqual([
      `${window.location.origin}/chargewire/v1/sessions/s_one`,
      `${window.location.origin}/chargewire/v1/sessions/s%2Ftwo`,
    ]);
    for (const { init } of fetches) {
      expect(init.method).toBe("DELETE");
      expect(init.keepalive).toBe(true);
      expect(new Headers(init.headers).get("Authorization")).toBe("Bearer tok");
    }
    window.dispatchEvent(pageEvent("pagehide"));
    expect(fetches).toHaveLength(2);
    // A session opened since (the page went on after all) is ended at the next exit.
    ids = ["s_one", "s_three"];
    window.dispatchEvent(pageEvent("pagehide"));
    expect(fetches.map((f) => f.url).at(-1)).toBe(`${window.location.origin}/chargewire/v1/sessions/s_three`);
  });

  it("ends them on beforeunload, unless the page asks the user first", () => {
    const stopWarning = warnBeforeLeaving();
    const asked = pageEvent("beforeunload");
    window.dispatchEvent(asked);
    expect(asked.defaultPrevented).toBe(true); // the browser asks
    expect(fetches).toHaveLength(0); // and the user may stay
    stopWarning();
    stopWarning(); // twice is once
    const left = pageEvent("beforeunload");
    window.dispatchEvent(left);
    expect(left.defaultPrevented).toBe(false);
    expect(fetches).toHaveLength(2);
  });

  it("loads the page again when it comes back from the back/forward cache with its sessions ended", () => {
    window.dispatchEvent(pageEvent("pageshow", true));
    expect(reload).not.toHaveBeenCalled(); // nothing was ended
    window.dispatchEvent(pageEvent("pagehide", true));
    window.dispatchEvent(pageEvent("pageshow", false));
    expect(reload).not.toHaveBeenCalled(); // a fresh load
    window.dispatchEvent(pageEvent("pageshow", true));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("does nothing while signed out", () => {
    stop();
    stop = endSessionsOnExit(() => undefined, reload);
    window.dispatchEvent(pageEvent("pagehide"));
    expect(fetches).toHaveLength(0);
  });

  it("awaits explicit session cleanup before the native close, once", async () => {
    stop();
    let finish!: () => void;
    const cleaned = new Promise<void>((resolve) => {
      finish = resolve;
    });
    const beforeClose = vi.fn(() => cleaned);
    const close = vi.spyOn(window, "close").mockImplementation(() => {});
    stop = endSessionsOnExit(() => ({ token: "tok", ids }), reload, beforeClose);
    requestWindowClose();
    requestWindowClose();
    expect(beforeClose).toHaveBeenCalledTimes(1);
    expect(close).not.toHaveBeenCalled();
    ids = []; // session close promises resolve only after their DELETEs
    finish();
    await vi.waitFor(() => expect(close).toHaveBeenCalledTimes(1));
    expect(fetches).toHaveLength(0);
    close.mockRestore();
  });

  it("finishes a native close even when a disconnected server never answers", async () => {
    stop();
    vi.useFakeTimers();
    const close = vi.spyOn(window, "close").mockImplementation(() => {});
    try {
      stop = endSessionsOnExit(
        () => ({ token: "tok", ids }),
        reload,
        () => new Promise(() => {}),
      );
      requestWindowClose();
      expect(close).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(5000);
      expect(close).toHaveBeenCalledTimes(1);
      expect(fetches).toHaveLength(2); // keepalive fallback for any known sessions
    } finally {
      vi.useRealTimers();
      close.mockRestore();
    }
  });

  it("keeps transaction sessions while confirmation is pending or cancelled, and can retry", async () => {
    stop();
    vi.useFakeTimers();
    const stopWarning = warnBeforeLeaving();
    const close = vi.spyOn(window, "close").mockImplementation(() => {});
    const cleanup = vi.fn(async () => {});
    let answer!: (close: boolean) => void;
    const confirm = vi.fn(
      () =>
        new Promise<boolean>((resolve) => {
          answer = resolve;
        }),
    );
    try {
      stop = endSessionsOnExit(() => ({ token: "tok", ids }), reload, cleanup, confirm);
      requestWindowClose();
      requestWindowClose();
      expect(confirm).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(10_000);
      expect(cleanup).not.toHaveBeenCalled();
      expect(close).not.toHaveBeenCalled();
      expect(fetches).toHaveLength(0);
      answer(false);
      await vi.advanceTimersByTimeAsync(0);
      requestWindowClose();
      expect(confirm).toHaveBeenCalledTimes(2);
      answer(true);
      await vi.advanceTimersByTimeAsync(0);
      expect(cleanup).toHaveBeenCalledTimes(1);
      expect(close).toHaveBeenCalledTimes(1);
      expect(fetches).toHaveLength(2);
      const leaving = pageEvent("beforeunload");
      window.dispatchEvent(leaving);
      expect(leaving.defaultPrevented).toBe(false); // do not ask twice after approval
    } finally {
      stopWarning();
      vi.useRealTimers();
      close.mockRestore();
    }
  });
});
