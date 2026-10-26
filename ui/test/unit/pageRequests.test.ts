import { describe, expect, it, vi } from "vitest";

import { createPageRequests } from "../../src/lib/pageRequests";

function pendingTransport() {
  return vi.fn<typeof fetch>(
    (_input, init) =>
      new Promise((_resolve, reject) => {
        const signal = init!.signal!;
        signal.addEventListener("abort", () => reject(signal.reason as Error), { once: true });
      }),
  );
}

describe("page request lifetime", () => {
  it("rejects work after exit without calling the detached page's fetch", async () => {
    const transport = pendingTransport();
    const page = createPageRequests(transport);
    page.abort();
    await expect(page.fetch("https://server/metadata")).rejects.toMatchObject({ name: "AbortError" });
    expect(transport).not.toHaveBeenCalled();
  });

  it("aborts pending metadata and query requests together on exit", async () => {
    const transport = pendingTransport();
    const page = createPageRequests(transport);
    const metadata = expect(page.fetch("https://server/metadata")).rejects.toMatchObject({ name: "AbortError" });
    const query = expect(page.fetch("https://server/statements", { method: "POST" })).rejects.toMatchObject({
      name: "AbortError",
    });
    page.abort();
    await Promise.all([metadata, query]);
  });

  it("preserves a caller's cancellation without cancelling other page requests", async () => {
    const transport = pendingTransport();
    const page = createPageRequests(transport);
    const caller = new AbortController();
    const query = expect(page.fetch("https://server/statements", { signal: caller.signal })).rejects.toMatchObject({
      name: "AbortError",
    });
    const metadata = expect(page.fetch("https://server/metadata")).rejects.toMatchObject({ name: "AbortError" });
    caller.abort();
    await query;
    expect(transport.mock.calls[1]![1]!.signal!.aborted).toBe(false);
    page.abort();
    await metadata;
  });
});
