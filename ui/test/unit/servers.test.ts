// What the servers module keeps in this browser (the list, this tab's sign-ins, the relay
// session), the desktop app's hand-over, the URL rules and the relay's fetch wrapper.
import { describe, expect, it, vi } from "vitest";

import {
  desktopServersKey,
  loadRemoteSignIns,
  loadSavedServers,
  mergeDesktopServers,
  relayFetch,
  saveRemoteSignIns,
  saveSavedServers,
  urlTlsMode,
} from "../../src/lib/servers";

class MemoryStorage implements Storage {
  #items = new Map<string, string>();
  get length() {
    return this.#items.size;
  }
  clear() {
    this.#items.clear();
  }
  getItem(key: string) {
    return this.#items.get(key) ?? null;
  }
  key(index: number) {
    return [...this.#items.keys()][index] ?? null;
  }
  removeItem(key: string) {
    this.#items.delete(key);
  }
  setItem(key: string, value: string) {
    this.#items.set(key, value);
  }
}

const broken = {
  getItem: () => {
    throw new Error("blocked");
  },
  setItem: () => {
    throw new Error("blocked");
  },
} as unknown as Storage;

const warehouse = { id: "s1", name: "Warehouse", url: "https://wh.example.com:31337", tls: "verified" as const };
const lab = {
  id: "s2",
  name: "Lab",
  url: "https://lab.local:31337",
  tls: "fingerprint" as const,
  fingerprint: "aa:bb",
};

describe("the saved list", () => {
  it("round-trips per user, keeps only well-formed entries, and survives blocked storage", () => {
    const storage = new MemoryStorage();
    saveSavedServers("joe", [warehouse, lab], storage);
    expect(loadSavedServers("joe", storage)).toEqual([warehouse, lab]);
    expect(loadSavedServers("ann", storage)).toEqual([]);
    storage.setItem(
      "hardcharger-ui:servers:v1:joe",
      JSON.stringify([warehouse, { id: "x", name: "no url" }, { ...lab, tls: "banana" }, "junk"]),
    );
    expect(loadSavedServers("joe", storage)).toEqual([warehouse]);
    storage.setItem("hardcharger-ui:servers:v1:joe", "{not json");
    expect(loadSavedServers("joe", storage)).toEqual([]);
    expect(loadSavedServers("joe", broken)).toEqual([]);
    expect(() => saveSavedServers("joe", [warehouse], broken)).not.toThrow();
  });

  it("never holds a password or token", () => {
    const storage = new MemoryStorage();
    saveSavedServers("joe", [{ ...warehouse, password: "secret", token: "t" } as never], storage);
    expect(storage.getItem("hardcharger-ui:servers:v1:joe")).not.toContain("secret");
    expect(loadSavedServers("joe", storage)[0]).not.toHaveProperty("password");
  });

  it("merges the desktop app's servers in by URL, the saved entry winning", () => {
    const handed = new MemoryStorage();
    handed.setItem(
      desktopServersKey,
      JSON.stringify([
        { id: "", name: "Warehouse (desktop)", url: "https://WH.example.com:31337/", tls: "verified" },
        { id: "d2", name: "Prod", url: "https://prod.example.com", tls: "fingerprint", fingerprint: "cc:dd" },
        { name: "broken" },
      ]),
    );
    const merged = mergeDesktopServers([warehouse], handed);
    expect(merged[0]).toEqual(warehouse);
    expect(merged[1]).toMatchObject({ id: "d2", name: "Prod", fingerprint: "cc:dd" });
    expect(merged).toHaveLength(2);
    expect(mergeDesktopServers([warehouse], broken)).toEqual([warehouse]);
  });
});

describe("this tab's sign-ins", () => {
  it("keeps only sign-ins that have not expired", () => {
    const storage = new MemoryStorage();
    const live = {
      serverId: "s1",
      relayId: "r-1",
      relayPath: "/ui/v1/remotes/r-1/chargewire",
      token: "tok",
      expiresAt: Date.now() + 60_000,
      username: "ann",
      serverName: "Hardcharger",
      version: "2.7.0",
      tls: null,
    };
    saveRemoteSignIns([live, { ...live, serverId: "s2", expiresAt: Date.now() - 1 }], storage);
    expect(loadRemoteSignIns(storage)).toEqual([live]);
    expect(loadRemoteSignIns(broken)).toEqual([]);
  });
});

describe("urlTlsMode", () => {
  it("reads the scheme", () => {
    expect(urlTlsMode("https://x")).toBe("verified");
    expect(urlTlsMode("  HTTP://x ")).toBe("plaintext");
    expect(urlTlsMode("chargewire://x")).toBe("verified");
    expect(urlTlsMode("x.example.com")).toBeUndefined();
  });
});

describe("relayFetch", () => {
  it("moves the remote's token to Chargewire-Relay-Authorization and adds the relay's own", async () => {
    const seen: Headers[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        seen.push(new Headers(init?.headers));
        return new Response("{}", { status: 200 });
      }),
    );
    try {
      const f = relayFetch(() => ({ token: "local", relaySession: "p-abc" }));
      await f("http://x/ui/v1/remotes/r-1/chargewire/v1/sessions", { headers: { Authorization: "Bearer remote" } });
      expect(seen[0]!.get("Authorization")).toBe("Bearer local");
      expect(seen[0]!.get("Chargewire-Relay-Authorization")).toBe("Bearer remote");
      expect(seen[0]!.get("Chargewire-Relay-Session")).toBe("p-abc");
      // Without a local sign-in (the UI on its own): only the relay session.
      const alone = relayFetch(() => ({ relaySession: "p-abc" }));
      await alone("http://x/ui/v1/remotes/r-1/chargewire", { headers: { Authorization: "Bearer remote" } });
      expect(seen[1]!.has("Authorization")).toBe(false);
      expect(seen[1]!.get("Chargewire-Relay-Authorization")).toBe("Bearer remote");
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
