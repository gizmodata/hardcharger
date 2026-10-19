// Saved queries: SQL in folders, kept in this browser (IndexedDB), per server origin and user.
// Hardcharger has no store of its own for them, so they don't follow a user to another
// browser; exporting the library to a file and importing it elsewhere moves them. Every
// access is guarded: without IndexedDB (a private window, blocked storage) the store says it
// is unavailable and the page works without it.

export interface SavedFolder {
  id: string;
  kind: "folder";
  name: string;
  parentId: string | null;
}

export interface SavedQuery {
  id: string;
  kind: "query";
  name: string;
  parentId: string | null;
  sql: string;
  catalog: string;
  schema: string;
  updatedAt: number;
}

export type SavedItem = SavedFolder | SavedQuery;

export const maxFolders = 1000;
export const maxQueries = 5000;
export const maxDepth = 20;
export const maxSqlBytes = 1024 * 1024;
const dbName = "hardcharger-ui";
const storeName = "saved-queries";
export const libraryFormat = "hardcharger-ui-saved-queries";

export class SavedQueryError extends Error {
  override readonly name = "SavedQueryError";
}

function newId(): string {
  return typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `q-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function request<T>(r: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error ?? new Error("IndexedDB request failed"));
  });
}

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    let r: IDBOpenDBRequest;
    try {
      r = indexedDB.open(dbName, 1);
    } catch (err) {
      reject(err instanceof Error ? err : new Error(String(err)));
      return;
    }
    r.onupgradeneeded = () => {
      const store = r.result.createObjectStore(storeName, { keyPath: ["user", "id"] });
      store.createIndex("user", "user");
    };
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error ?? new Error("IndexedDB is unavailable"));
    r.onblocked = () => reject(new Error("IndexedDB is blocked by another tab"));
  });
}

/** Names compare without case, as a person reads them. */
const sameName = (a: string, b: string) => a.localeCompare(b, undefined, { sensitivity: "accent" }) === 0;

/** One user's saved queries in this browser. */
export class SavedQueryStore {
  readonly #user: string;
  #db: Promise<IDBDatabase> | undefined;

  constructor(user: string) {
    this.#user = user;
  }

  #open(): Promise<IDBDatabase> {
    this.#db ??= openDb().catch((err: unknown) => {
      this.#db = undefined;
      throw err;
    });
    return this.#db;
  }

  /** Whether this browser lets the page keep saved queries. */
  async available(): Promise<boolean> {
    try {
      await this.#open();
      return true;
    } catch {
      return false;
    }
  }

  async list(): Promise<SavedItem[]> {
    const db = await this.#open();
    const tx = db.transaction(storeName, "readonly");
    const rows = (await request(tx.objectStore(storeName).index("user").getAll(this.#user))) as (SavedItem & {
      user: string;
    })[];
    return rows.map(({ user: _user, ...item }): SavedItem => item);
  }

  async #write(items: SavedItem[], remove: string[] = []): Promise<void> {
    const db = await this.#open();
    const tx = db.transaction(storeName, "readwrite");
    const store = tx.objectStore(storeName);
    for (const item of items) store.put({ ...item, user: this.#user });
    for (const id of remove) store.delete([this.#user, id]);
    await new Promise<void>((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error ?? new Error("IndexedDB write failed"));
      tx.onabort = () => reject(tx.error ?? new Error("IndexedDB write aborted"));
    });
  }

  /** Checks a name for `parentId` (unique there, not empty), ignoring `self`. */
  static checkName(items: readonly SavedItem[], name: string, parentId: string | null, self?: string): string {
    const n = name.trim();
    if (!n) throw new SavedQueryError("A name can't be empty.");
    if (n.length > 200) throw new SavedQueryError("A name can be at most 200 characters.");
    if (items.some((i) => i.parentId === parentId && i.id !== self && sameName(i.name, n))) {
      throw new SavedQueryError(`"${n}" is already in this folder.`);
    }
    return n;
  }

  static depth(items: readonly SavedItem[], folderId: string | null): number {
    let d = 0;
    for (let id = folderId; id; id = items.find((i) => i.id === id)?.parentId ?? null) d++;
    return d;
  }

  async createFolder(name: string, parentId: string | null): Promise<SavedFolder> {
    const items = await this.list();
    if (items.filter((i) => i.kind === "folder").length >= maxFolders) {
      throw new SavedQueryError(`At most ${maxFolders.toLocaleString()} folders.`);
    }
    if (SavedQueryStore.depth(items, parentId) >= maxDepth)
      throw new SavedQueryError(`Folders go at most ${maxDepth} deep.`);
    const folder: SavedFolder = {
      id: newId(),
      kind: "folder",
      name: SavedQueryStore.checkName(items, name, parentId),
      parentId,
    };
    await this.#write([folder]);
    return folder;
  }

  /** Saves new SQL as a query, or (with `id`) updates the query that has it. */
  async saveQuery(q: {
    id?: string;
    name: string;
    parentId: string | null;
    sql: string;
    catalog: string;
    schema: string;
  }): Promise<SavedQuery> {
    if (new TextEncoder().encode(q.sql).length > maxSqlBytes)
      throw new SavedQueryError("A saved query can be at most 1 MB of SQL.");
    const items = await this.list();
    if (!q.id && items.filter((i) => i.kind === "query").length >= maxQueries) {
      throw new SavedQueryError(`At most ${maxQueries.toLocaleString()} saved queries.`);
    }
    const query: SavedQuery = {
      id: q.id ?? newId(),
      kind: "query",
      name: SavedQueryStore.checkName(items, q.name, q.parentId, q.id),
      parentId: q.parentId,
      sql: q.sql,
      catalog: q.catalog,
      schema: q.schema,
      updatedAt: Date.now(),
    };
    await this.#write([query]);
    return query;
  }

  async rename(id: string, name: string): Promise<SavedItem> {
    const items = await this.list();
    const item = items.find((i) => i.id === id);
    if (!item) throw new SavedQueryError("It is gone (deleted in another tab?).");
    const renamed = { ...item, name: SavedQueryStore.checkName(items, name, item.parentId, id) };
    await this.#write([renamed]);
    return renamed;
  }

  /** Moves an item into a folder (null: the top level); a folder never into its own sub-folders. */
  async move(id: string, parentId: string | null): Promise<void> {
    const items = await this.list();
    const item = items.find((i) => i.id === id);
    if (!item || item.parentId === parentId) return;
    if (item.kind === "folder") {
      for (let p = parentId; p; p = items.find((i) => i.id === p)?.parentId ?? null) {
        if (p === id) throw new SavedQueryError("A folder can't go inside itself.");
      }
      if (SavedQueryStore.depth(items, parentId) + 1 > maxDepth)
        throw new SavedQueryError(`Folders go at most ${maxDepth} deep.`);
    }
    await this.#write([{ ...item, name: SavedQueryStore.checkName(items, item.name, parentId, id), parentId }]);
  }

  /** Deletes an item; a folder with everything in it. Returns the ids deleted. */
  async remove(id: string): Promise<string[]> {
    const items = await this.list();
    const gone = descendants(items, id);
    await this.#write([], gone);
    return gone;
  }

  /** Adds .sql files as queries in a folder; a name taken gets " (2)", " (3)", ... */
  async importFiles(files: { name: string; sql: string }[], parentId: string | null): Promise<SavedQuery[]> {
    const items = await this.list();
    const added: SavedQuery[] = [];
    for (const f of files) {
      if (new TextEncoder().encode(f.sql).length > maxSqlBytes) throw new SavedQueryError(`${f.name} is over 1 MB.`);
      const base = f.name.replace(/\.sql$/i, "").trim() || "Query";
      const name = freeName([...items, ...added], base, parentId);
      added.push({
        id: newId(),
        kind: "query",
        name,
        parentId,
        sql: f.sql,
        catalog: "",
        schema: "",
        updatedAt: Date.now(),
      });
    }
    if (items.filter((i) => i.kind === "query").length + added.length > maxQueries) {
      throw new SavedQueryError(`At most ${maxQueries.toLocaleString()} saved queries.`);
    }
    await this.#write(added);
    return added;
  }

  /** The whole library as a file's text (to move it to another browser). */
  async exportLibrary(): Promise<string> {
    return JSON.stringify(
      { format: libraryFormat, version: 1, exportedAt: new Date().toISOString(), items: await this.list() },
      null,
      2,
    );
  }

  /**
   * Adds an exported library to this one, keeping its folders: top-level items whose names are
   * taken get " (2)", " (3)", ... Returns how many items were added.
   */
  async importLibrary(text: string): Promise<number> {
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      throw new SavedQueryError("That file isn't a saved-queries export (not JSON).");
    }
    const doc = parsed as { format?: unknown; items?: unknown };
    if (doc.format !== libraryFormat || !Array.isArray(doc.items)) {
      throw new SavedQueryError("That file isn't a saved-queries export.");
    }
    const incoming = (doc.items as unknown[]).filter(isItem);
    const items = await this.list();
    const ids = new Map<string, string>();
    for (const i of incoming) ids.set(i.id, newId());
    const added: SavedItem[] = [];
    for (const i of incoming) {
      const parentId = i.parentId && ids.has(i.parentId) ? ids.get(i.parentId)! : null;
      const name = parentId ? i.name : freeName([...items, ...added], i.name, null);
      added.push({ ...i, id: ids.get(i.id)!, parentId, name });
    }
    if (items.filter((i) => i.kind === "query").length + added.filter((i) => i.kind === "query").length > maxQueries) {
      throw new SavedQueryError(`At most ${maxQueries.toLocaleString()} saved queries.`);
    }
    await this.#write(added);
    return added.length;
  }
}

function isItem(x: unknown): x is SavedItem {
  const r = x as Record<string, unknown> | null;
  if (!r || typeof r["id"] !== "string" || typeof r["name"] !== "string") return false;
  if (r["parentId"] !== null && typeof r["parentId"] !== "string") return false;
  if (r["kind"] === "folder") return true;
  return r["kind"] === "query" && typeof r["sql"] === "string";
}

/** `id` and everything under it. */
export function descendants(items: readonly SavedItem[], id: string): string[] {
  const out = [id];
  for (let i = 0; i < out.length; i++) for (const c of items) if (c.parentId === out[i]) out.push(c.id);
  return out;
}

/** `base`, or `base (2)`, `base (3)`, ...: the first name free in `parentId`. */
export function freeName(items: readonly SavedItem[], base: string, parentId: string | null): string {
  const taken = (n: string) => items.some((i) => i.parentId === parentId && sameName(i.name, n));
  if (!taken(base)) return base;
  for (let n = 2; ; n++) if (!taken(`${base} (${n})`)) return `${base} (${n})`;
}

/** A file name for a query's .sql download, or a folder's path in a .zip. */
export function fileName(name: string): string {
  // eslint-disable-next-line no-control-regex
  return name.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_").trim() || "query";
}

const stores = new Map<string, SavedQueryStore>();
const listeners = new Set<() => void>();

/** The saved-query store of `user` in this browser (one per user, shared by the page). */
export function savedQueryStore(user: string): SavedQueryStore {
  let store = stores.get(user);
  if (!store) {
    store = new SavedQueryStore(user);
    stores.set(user, store);
  }
  return store;
}

/** Says the saved queries changed (the panel lists them again, tabs follow renames). */
export function savedQueriesChanged(): void {
  for (const l of listeners) l();
}

export function onSavedQueriesChanged(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The folder new queries and folders go in (the panel's selection), shared with Save. */
let selectedFolder: string | null = null;
export const getSelectedFolder = (): string | null => selectedFolder;
export const setSelectedFolder = (id: string | null): void => {
  selectedFolder = id;
};
