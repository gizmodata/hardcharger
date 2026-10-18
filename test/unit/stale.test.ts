import { describe, expect, it, vi } from "vitest";

import { StaleBuildError, isChunkLoadError, loadModule, recover, recovered, reloadDelaysMs } from "../../src/lib/stale";

function memoryStorage(): Storage {
  const m = new Map<string, string>();
  return {
    get length() {
      return m.size;
    },
    clear: () => m.clear(),
    getItem: (k) => m.get(k) ?? null,
    key: (i) => [...m.keys()][i] ?? null,
    removeItem: (k) => void m.delete(k),
    setItem: (k, v) => void m.set(k, v),
  };
}

describe("stale-build recovery", () => {
  it("reloads after 0, 3 and 10 seconds, then gives up", () => {
    const storage = memoryStorage();
    const scheduled: number[] = [];
    const reload = vi.fn();
    const schedule = (fn: () => void, ms: number) => {
      scheduled.push(ms);
      fn();
    };
    const steps = [0, 1, 2, 3].map((i) => recover(reload, { storage, now: 1000 + i, schedule }));
    expect(steps.map((s) => s.kind)).toEqual(["reloading", "reloading", "reloading", "gave-up"]);
    expect(scheduled).toEqual([...reloadDelaysMs]);
    expect(reload).toHaveBeenCalledTimes(3);
  });

  it("starts over after a while, or once the page recovered", () => {
    const storage = memoryStorage();
    const schedule = () => {};
    for (let i = 0; i < 3; i++) recover(() => {}, { storage, now: 0, schedule });
    expect(recover(() => {}, { storage, now: 1000, schedule }).kind).toBe("gave-up");
    expect(recover(() => {}, { storage, now: 10 * 60_000, schedule })).toMatchObject({ attempt: 1, delayMs: 0 });
    recovered(storage);
    expect(recover(() => {}, { storage, now: 10 * 60_000, schedule })).toMatchObject({ attempt: 1 });
  });

  it("without storage, reloads once and then gives up", () => {
    const schedule = vi.fn();
    expect(recover(() => {}, { storage: undefined, now: 0, schedule }).kind).toBe("reloading");
  });

  it("recognizes each browser's failed module load", () => {
    expect(isChunkLoadError(new TypeError("Failed to fetch dynamically imported module: http://x/a.js"))).toBe(true);
    expect(isChunkLoadError(new TypeError("Importing a module script failed."))).toBe(true);
    expect(isChunkLoadError(new TypeError("error loading dynamically imported module"))).toBe(true);
    expect(isChunkLoadError(new Error("Unable to preload CSS for /ui/assets/x.css"))).toBe(true);
    expect(isChunkLoadError(new Error("something else"))).toBe(false);
  });

  it("turns a failed import into a StaleBuildError and passes other errors through", async () => {
    await expect(
      loadModule("a", () => Promise.reject(new TypeError("Importing a module script failed.")))(),
    ).rejects.toBeInstanceOf(StaleBuildError);
    await expect(loadModule("a", () => Promise.reject(new Error("boom")))()).rejects.toThrow("boom");
    await expect(loadModule("a", () => Promise.resolve(42))()).resolves.toBe(42);
  });
});
