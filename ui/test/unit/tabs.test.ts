import { describe, expect, it } from "vitest";

import { hasUnsavedText, loadTabs, makeTab, maxTabs, nextTitle, saveTabs } from "../../src/lib/tabs";

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
  key(i: number) {
    return [...this.#items.keys()][i] ?? null;
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

describe("tab titles", () => {
  it("takes the lowest number no open tab uses", () => {
    const one = makeTab([]);
    expect(one.title).toBe("Query 1");
    const three = makeTab([one], { title: "Query 3" });
    expect(nextTitle([one, three])).toBe("Query 2");
    expect(nextTitle([three])).toBe("Query 1");
  });
});

describe("hasUnsavedText", () => {
  it("is any text for a tab that isn't a saved query, and a change for one that is", () => {
    expect(hasUnsavedText(makeTab([], { sql: "  \n" }))).toBe(false);
    expect(hasUnsavedText(makeTab([], { sql: "SELECT 1" }))).toBe(true);
    expect(hasUnsavedText(makeTab([], { sql: "SELECT 1", savedQueryId: "q", savedSql: "SELECT 1" }))).toBe(false);
    expect(hasUnsavedText(makeTab([], { sql: "SELECT 2", savedQueryId: "q", savedSql: "SELECT 1" }))).toBe(true);
  });
});

describe("loadTabs and saveTabs", () => {
  it("round-trips per user", () => {
    const storage = new MemoryStorage();
    const a = makeTab([], { sql: "SELECT 'a'", catalog: "lake", schema: "s", rowLimit: 5000 });
    const b = makeTab([a], { sql: "SELECT 'b'" });
    saveTabs("joe", { tabs: [a, b], active: b.id }, storage);
    expect(loadTabs("joe", storage)).toEqual({ tabs: [a, b], active: b.id });
    expect(loadTabs("ann", storage)).toBeNull();
  });

  it("keeps at most 20 valid tabs, an active one among them, and a known row limit", () => {
    const storage = new MemoryStorage();
    const tabs = Array.from({ length: 25 }, (_, i) => ({ ...makeTab([]), title: `T${i}` }));
    storage.setItem(
      "hardcharger-ui:tabs:v1:joe",
      JSON.stringify({
        tabs: [
          { id: "../bad", title: "x", sql: "" },
          { id: "ok", title: 3 },
          ...tabs.map((t) => ({ ...t, rowLimit: 7 })),
        ],
        active: "gone",
      }),
    );
    const loaded = loadTabs("joe", storage)!;
    expect(loaded.tabs).toHaveLength(maxTabs);
    expect(loaded.active).toBe(loaded.tabs[0]!.id);
    expect(loaded.tabs.every((t) => t.rowLimit === 1000)).toBe(true);
  });

  it("works without storage (private windows, blocked storage)", () => {
    expect(loadTabs("joe", broken)).toBeNull();
    expect(() => saveTabs("joe", { tabs: [makeTab([])], active: "x" }, broken)).not.toThrow();
    expect(loadTabs("joe", undefined)).toBeNull();
  });

  it("shortens a giant text before storing it", () => {
    const storage = new MemoryStorage();
    const tab = makeTab([], { sql: "x".repeat(600 * 1024) });
    saveTabs("joe", { tabs: [tab], active: tab.id }, storage);
    expect(loadTabs("joe", storage)!.tabs[0]!.sql).toHaveLength(512 * 1024);
  });
});
