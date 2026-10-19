// The query page's editor tabs: several editors open at once, each with its own Warp SQL
// session (so a transaction open in one tab never touches another), its own catalog,
// schema, row limit and results.
//
// The open tabs and their text are kept in this browser (localStorage), per server origin
// and user, so a reload brings them back. Every storage access is guarded: in a private
// window, or with storage blocked, tabs just don't survive a reload.

export const maxTabs = 20;
export const rowLimits = [100, 1000, 5000, 10_000, 100_000, 1_000_000] as const;
export const defaultRowLimit = 1000;
/** Per tab, so one giant paste can't push every other tab out of storage. */
const maxStoredSqlChars = 512 * 1024;
const storagePrefix = "hardcharger-ui:tabs:v1";

export interface TabDoc {
  id: string;
  title: string;
  sql: string;
  /** The catalog and schema the tab's session uses ("": the user's default). */
  catalog: string;
  schema: string;
  /** The role the tab acts with (Run as); "" for all of the user's roles. */
  role: string;
  rowLimit: number;
  /** The saved query this tab edits, and its text as last saved (for the unsaved dot). */
  savedQueryId?: string;
  savedSql?: string;
}

export interface StoredTabs {
  tabs: TabDoc[];
  active: string;
}

export function newId(): string {
  return typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `t-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

/** "Query 1", "Query 2", …: the lowest number no open tab uses. */
export function nextTitle(tabs: readonly TabDoc[]): string {
  const used = new Set(tabs.map((t) => t.title));
  for (let n = 1; ; n++) if (!used.has(`Query ${n}`)) return `Query ${n}`;
}

export function makeTab(tabs: readonly TabDoc[], init: Partial<TabDoc> = {}): TabDoc {
  return {
    id: newId(),
    title: nextTitle(tabs),
    sql: "",
    catalog: "",
    schema: "",
    role: "",
    rowLimit: defaultRowLimit,
    ...init,
  };
}

/** True when the tab has text that isn't in a saved query (closing it would lose it). */
export function hasUnsavedText(tab: TabDoc): boolean {
  return tab.savedQueryId ? tab.sql !== (tab.savedSql ?? "") : tab.sql.trim() !== "";
}

const storageKey = (user: string) => `${storagePrefix}:${user}`;

function isTab(t: unknown): t is TabDoc {
  if (t === null || typeof t !== "object") return false;
  const r = t as Record<string, unknown>;
  return (
    typeof r["id"] === "string" &&
    /^[A-Za-z0-9_-]{1,64}$/.test(r["id"]) &&
    typeof r["title"] === "string" &&
    typeof r["sql"] === "string"
  );
}

function normalized(t: TabDoc): TabDoc {
  const limit: unknown = t.rowLimit;
  return {
    ...t,
    catalog: typeof t.catalog === "string" ? t.catalog : "",
    schema: typeof t.schema === "string" ? t.schema : "",
    role: typeof t.role === "string" ? t.role : "",
    rowLimit: typeof limit === "number" && (rowLimits as readonly number[]).includes(limit) ? limit : defaultRowLimit,
  };
}

export function loadTabs(user: string, storage: Storage | undefined = local()): StoredTabs | null {
  try {
    const raw = storage?.getItem(storageKey(user));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { tabs?: unknown[]; active?: unknown };
    const tabs = (parsed.tabs ?? []).filter(isTab).slice(0, maxTabs).map(normalized);
    if (tabs.length === 0) return null;
    const active = tabs.some((t) => t.id === parsed.active) ? (parsed.active as string) : tabs[0]!.id;
    return { tabs, active };
  } catch {
    return null;
  }
}

export function saveTabs(user: string, value: StoredTabs, storage: Storage | undefined = local()): void {
  try {
    const tabs = value.tabs.map((t) => ({
      ...t,
      sql: t.sql.length > maxStoredSqlChars ? t.sql.slice(0, maxStoredSqlChars) : t.sql,
    }));
    storage?.setItem(storageKey(user), JSON.stringify({ ...value, tabs }));
  } catch {
    // storage full or blocked: the tabs just won't survive a reload
  }
}

function local(): Storage | undefined {
  try {
    return window.localStorage;
  } catch {
    return undefined;
  }
}
