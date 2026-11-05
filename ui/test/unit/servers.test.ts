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
  loadDismissed,
  loadTabServers,
  normalizeSaved,
  saveDismissed,
  ServersStore,
  saveTabServers,
  serverKey,
  tlsModeOf,
  urlOf,
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

const warehouse = {
  id: "s1",
  name: "Warehouse",
  host: "wh.example.com",
  port: 31337,
  tls: true,
  prefix: "",
  trust: false,
  username: "ann",
  timeout: 30,
};
const lab = {
  id: "s2",
  name: "Lab",
  host: "lab.local",
  port: 31337,
  tls: true,
  prefix: "/cw",
  trust: true,
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
      JSON.stringify([warehouse, { id: "x", name: "no host" }, { ...lab, port: 99999 }, "junk"]),
    );
    expect(loadSavedServers("joe", storage)).toEqual([warehouse]);
    storage.setItem("hardcharger-ui:servers:v1:joe", "{not json");
    expect(loadSavedServers("joe", storage)).toEqual([]);
    expect(loadSavedServers("joe", broken)).toEqual([]);
    expect(() => saveSavedServers("joe", [warehouse], broken)).not.toThrow();
  });

  it("keeps a resume timeout, 0 included (don't wait), and drops one that isn't a count of seconds", () => {
    const storage = new MemoryStorage();
    const waits = { ...warehouse, id: "r1", host: "a.example.com", resumeTimeout: 120 };
    const never = { ...warehouse, id: "r2", host: "b.example.com", resumeTimeout: 0 };
    saveSavedServers("joe", [waits, never, lab], storage);
    const loaded = loadSavedServers("joe", storage);
    expect(loaded.map((s) => s.resumeTimeout)).toEqual([120, 0, undefined]); // absent: the default, 600
    expect(normalizeSaved({ ...warehouse, resumeTimeout: -5 })).not.toHaveProperty("resumeTimeout");
    expect(normalizeSaved({ ...warehouse, resumeTimeout: "60" })).not.toHaveProperty("resumeTimeout");
    expect(normalizeSaved({ ...warehouse, resumeTimeout: 90.7 })).toMatchObject({ resumeTimeout: 90 });
  });

  it("reads entries saved as a URL (the first 2.7.0 builds) as host, port and trust", () => {
    expect(normalizeSaved({ id: "a", name: "A", url: "https://wh.example.com:31337", tls: "verified" })).toEqual({
      id: "a",
      name: "A",
      host: "wh.example.com",
      port: 31337,
      tls: true,
      prefix: "",
      trust: false,
    });
    expect(
      normalizeSaved({ id: "b", name: "B", url: "https://[::1]:4433/cw", tls: "fingerprint", fingerprint: "cc:dd" }),
    ).toMatchObject({ host: "::1", port: 4433, prefix: "/cw", tls: true, trust: true, fingerprint: "cc:dd" });
    expect(normalizeSaved({ id: "c", name: "C", url: "http://h:80", tls: "plaintext" })).toMatchObject({
      tls: false,
      trust: false,
      port: 80,
    });
    expect(normalizeSaved({ id: "d", name: "D", url: "https://h", tls: "banana" })).toBeUndefined();
    expect(normalizeSaved({ id: "e", name: "E", url: "not a url", tls: "verified" })).toBeUndefined();
  });

  it("derives the relay URL and TLS mode, and one key per server", () => {
    expect(urlOf(lab)).toBe("https://lab.local:31337/cw");
    expect(tlsModeOf(warehouse)).toBe("verified");
    expect(tlsModeOf(lab)).toBe("fingerprint");
    expect(tlsModeOf({ tls: false, trust: true })).toBe("plaintext");
    expect(serverKey({ ...warehouse, host: "WH.example.com" })).toBe(serverKey(warehouse));
    expect(serverKey({ ...warehouse, prefix: "/chargewire" })).toBe(serverKey(warehouse));
  });

  it("keeps a connection not saved for later out of localStorage, for this tab only", () => {
    const storage = new MemoryStorage();
    const tab = new MemoryStorage();
    const once = { ...lab, id: "s3", transient: true };
    saveSavedServers("joe", [warehouse, once], storage);
    expect(loadSavedServers("joe", storage)).toEqual([warehouse]);
    saveTabServers([warehouse, once], tab);
    expect(loadTabServers(tab)).toEqual([{ ...lab, id: "s3", transient: true }]);
  });

  it("never holds a password or token", () => {
    const storage = new MemoryStorage();
    saveSavedServers("joe", [{ ...warehouse, password: "secret", token: "t" } as never], storage);
    expect(storage.getItem("hardcharger-ui:servers:v1:joe")).not.toContain("secret");
    expect(loadSavedServers("joe", storage)[0]).not.toHaveProperty("password");
  });

  it("merges the desktop app's servers in by host, port and prefix, the saved entry winning", () => {
    const handed = new MemoryStorage();
    handed.setItem(
      desktopServersKey,
      JSON.stringify([
        {
          id: "",
          name: "Warehouse (desktop)",
          host: "WH.example.com",
          port: 31337,
          tls: true,
          prefix: "",
          trust: false,
        },
        {
          id: "d2",
          name: "Prod",
          host: "prod.example.com",
          port: 443,
          tls: true,
          prefix: "",
          trust: true,
          fingerprint: "cc:dd",
        },
        { id: "d3", name: "Old", url: "http://old.example.com:31337", tls: "plaintext" },
        { name: "broken" },
      ]),
    );
    const merged = mergeDesktopServers([warehouse], handed);
    expect(merged[0]).toEqual(warehouse);
    expect(merged[1]).toMatchObject({ id: "d2", name: "Prod", trust: true, fingerprint: "cc:dd" });
    expect(merged[2]).toMatchObject({ id: "d3", host: "old.example.com", tls: false });
    expect(merged).toHaveLength(3);
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

describe("servers removed from the list", () => {
  const handed = () => {
    const storage = new MemoryStorage();
    storage.setItem(
      desktopServersKey,
      JSON.stringify([
        { id: "d1", name: "Vaden", host: "vaden.example.com", port: 31337, tls: true, prefix: "", trust: false },
        { id: "d2", name: "Lab", host: "lab.local", port: 31337, tls: true, prefix: "/cw", trust: false },
      ]),
    );
    return storage;
  };

  it("a dismissed desktop server isn't merged back in at the next load", () => {
    const dismissed = new Set([
      serverKey({ host: "VADEN.example.com", port: 31337, tls: true, prefix: "/chargewire" }),
    ]);
    const merged = mergeDesktopServers([], handed(), dismissed);
    expect(merged.map((s) => s.id)).toEqual(["d2"]);
    // Without the dismissal it comes in, as before.
    expect(mergeDesktopServers([], handed()).map((s) => s.id)).toEqual(["d1", "d2"]);
  });

  it("keeps the dismissed keys per user, survives blocked storage, and forgets an empty set", () => {
    const storage = new MemoryStorage();
    saveDismissed("joe", new Set(["a|1|", "b|2|"]), storage);
    expect([...loadDismissed("joe", storage)].sort()).toEqual(["a|1|", "b|2|"]);
    expect(loadDismissed("ann", storage).size).toBe(0);
    saveDismissed("joe", new Set(), storage);
    expect(storage.getItem("hardcharger-ui:dismissed-servers:v1:joe")).toBeNull();
    storage.setItem("hardcharger-ui:dismissed-servers:v1:joe", "{bad");
    expect(loadDismissed("joe", storage).size).toBe(0);
    expect(loadDismissed("joe", broken).size).toBe(0);
    expect(() => saveDismissed("joe", new Set(["x"]), broken)).not.toThrow();
  });
});

describe("ServersStore: removing a desktop server, and adding it back", () => {
  it("stays removed across loads until the same server is added again", () => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.sessionStorage.setItem(
      desktopServersKey,
      JSON.stringify([
        { id: "d1", name: "Vaden", host: "vaden.example.com", port: 31337, tls: true, prefix: "", trust: false },
      ]),
    );
    const first = new ServersStore("joe", () => undefined);
    expect(first.snapshot().servers.map((s) => s.saved.id)).toEqual(["d1"]);
    first.remove("d1");
    expect(first.snapshot().servers).toEqual([]);
    // The next load (the hand-over still in sessionStorage) doesn't bring it back.
    const second = new ServersStore("joe", () => undefined);
    expect(second.snapshot().servers).toEqual([]);
    // Adding it again clears the removal.
    const added = second.add({
      name: "Vaden",
      host: "vaden.example.com",
      port: 31337,
      tls: true,
      prefix: "",
      trust: false,
    });
    expect(new ServersStore("joe", () => undefined).snapshot().servers.map((s) => s.saved.id)).toEqual([added.id]);
  });

  it("reuses the picked entry when its address was edited, so one server never shows twice", () => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    const store = new ServersStore("joe", () => undefined);
    const picked = store.add({
      name: "Vaden",
      host: "vaden.example.com",
      port: 443,
      tls: true,
      prefix: "",
      trust: false,
    });
    const edited = store.add(
      { name: "Vaden", host: "vaden.example.com", port: 31337, tls: true, prefix: "", trust: false },
      picked.id,
    );
    expect(edited.id).toBe(picked.id);
    expect(store.snapshot().servers.map((s) => [s.saved.id, s.saved.port])).toEqual([[picked.id, 31337]]);
  });
});
