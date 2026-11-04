import { ChevronDown, HelpCircle, Plus, X } from "lucide-react";
import { confirmDialog, notifyDialog } from "../lib/dialogs";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { publishActiveTab, resetActiveTab } from "../lib/activeTab";
import { clearCatalogCaches, onCatalogsChanged } from "../lib/catalogs";
import { warnBeforeLeaving } from "../lib/pageExit";
import { grantedRoles } from "../lib/roles";
import {
  type SavedItem,
  getSelectedFolder,
  onSavedQueriesChanged,
  savedQueriesChanged,
  savedQueryStore,
} from "../lib/savedQueries";
import { useStoredSize } from "../lib/storedSize";
import { type StoredTabs, type TabDoc, hasUnsavedText, loadTabs, makeTab, maxTabs, saveTabs } from "../lib/tabs";
import type { Where } from "../lib/tabSession";
import type { ThemeName } from "../lib/theme";
import { localServerId, serverLabel, useServers } from "../lib/servers";
import { type QueryRequest, useSession } from "../session";
import { EditorTab, type TabHandle, type TabStatus } from "./EditorTab";
import { FloatingMenu, type MenuItem } from "./FloatingMenu";
import { HelpDialog } from "./HelpDialog";
import { ResizeHandle } from "./ResizeHandle";
import { SaveQueryDialog } from "./SaveQueryDialog";
import "../styles/query.css";

const firstSql = "SELECT HARDCHARGER_VERSION() AS version, HARDCHARGER_USER() AS me;\n";
const editorSize = {
  key: "hardcharger-ui:editor-height",
  defaultPx: 240,
  minPx: 100,
  maxPx: () => Math.max(100, Math.round(window.innerHeight * 0.7)),
};

const narrowNoteKey = "hardcharger-ui:narrow-note-hidden";

function narrowNoteHidden(): boolean {
  try {
    return window.localStorage.getItem(narrowNoteKey) === "1";
  } catch {
    return false;
  }
}

function initialTabs(user: string): StoredTabs {
  const saved = loadTabs(user);
  if (saved) return saved;
  const first = makeTab([], { sql: firstSql });
  return { tabs: [first], active: first.id };
}

interface Props {
  theme: ThemeName;
}

/**
 * The query page: editor tabs (up to 20), each with its own Chargewire session. New with + or
 * Alt+N, close with × or Alt+W (asking first when a transaction is open or text is unsaved),
 * rename with a double-click, reorder by dragging.
 */
export default function QueryPage({ theme }: Props) {
  const session = useSession();
  const user = session.identity.user;
  const [{ tabs, active }, setTabs] = useState(() => initialTabs(user));
  const [statuses, setStatuses] = useState<Record<string, TabStatus>>({});
  const [renaming, setRenaming] = useState<string | undefined>();
  const [runOnOpen, setRunOnOpen] = useState<Set<string>>(new Set());
  const [defaults, setDefaults] = useState<Where>({ catalog: "", schema: "" });
  const [catalogVersion, setCatalogVersion] = useState(0);
  const [editorHeight, setEditorHeight, resetEditorHeight] = useStoredSize(editorSize);
  const [help, setHelp] = useState(false);
  const [idleTimeoutS, setIdleTimeoutS] = useState<number | undefined>();
  // The servers a new tab can open on, and each tab's server's name.
  const servers = useServers(session.servers);
  const serverName = useCallback(
    (id: string): string | undefined => {
      if (!id) return undefined;
      const state = servers.servers.find((s) => s.saved.id === id);
      return state ? serverLabel(state) : "a removed server";
    },
    [servers],
  );
  const [serverMenu, setServerMenu] = useState<{ x: number; y: number } | null>(null);
  // On a phone-sized screen a note says the editor is easier on a larger one (CSS shows it
  // only there); its × hides it in this browser.
  const [noteHidden, setNoteHidden] = useState(narrowNoteHidden);
  const hideNote = () => {
    setNoteHidden(true);
    try {
      window.localStorage.setItem(narrowNoteKey, "1");
    } catch {
      // hidden until the page reloads
    }
  };
  const handles = useRef(new Map<string, TabHandle>());
  const activeRef = useRef(active);
  const dragged = useRef<string | undefined>(undefined);

  useEffect(() => {
    activeRef.current = active;
    saveTabs(user, { tabs, active });
  }, [tabs, active, user]);

  // The tabs' catalog and schema pickers list again whenever the listings are cleared: after a
  // run or an upload that may have changed the catalog, and on the schema browser's Refresh.
  useEffect(() => onCatalogsChanged(() => setCatalogVersion((v) => v + 1)), []);

  // The user's own catalog and schema: what a tab that picked none starts on.
  useEffect(() => {
    let live = true;
    session.connection
      .getSession()
      .then((doc) => {
        const o = doc.options ?? {};
        if (live) {
          setIdleTimeoutS(doc.idle_timeout_s);
          setDefaults({
            catalog: typeof o["catalog"] === "string" ? o["catalog"] : "",
            schema: typeof o["schema"] === "string" ? o["schema"] : "",
          });
        }
      })
      .catch((err: unknown) => session.checkSignIn(err));
    return () => {
      live = false;
    };
  }, [session, user]);

  // The roles the user may act with (Run as): none for an administrator or without access control.
  const [roles, setRoles] = useState<string[]>([]);
  useEffect(() => {
    let live = true;
    if (!session.localServer) return;
    grantedRoles(session.connection, user, session.isAdmin).then(
      (r) => live && setRoles(r),
      () => {},
    );
    return () => {
      live = false;
    };
  }, [session, user]);

  // Leaving the page with a transaction open loses it: the browser asks first.
  const anyTransaction = Object.values(statuses).some((s) => s.transaction !== "none");
  useEffect(() => (anyTransaction ? warnBeforeLeaving() : undefined), [anyTransaction]);

  const patchTab = useCallback(
    (id: string, patch: Partial<TabDoc>) =>
      setTabs((s) => ({ ...s, tabs: s.tabs.map((t) => (t.id === id ? { ...t, ...patch } : t)) })),
    [],
  );

  // The tabs as last rendered, for adding one: its id must be known at once (a preview runs
  // in it), and a state updater may run later than the call that queued it.
  const tabsNow = useRef({ tabs, active });
  useEffect(() => {
    tabsNow.current = { tabs, active };
  }, [tabs, active]);

  const addTab = useCallback((init: Partial<TabDoc> = {}): string | undefined => {
    const now = tabsNow.current;
    if (now.tabs.length >= maxTabs) return undefined;
    const current = now.tabs.find((t) => t.id === now.active);
    // A tab opens on the active tab's server; on another server, with that server's own
    // catalog and schema (the session's defaults there).
    const sameServer = (init.server ?? current?.server ?? "") === (current?.server ?? "");
    const tab = makeTab(now.tabs, {
      catalog: sameServer ? (current?.catalog ?? "") : "",
      schema: sameServer ? (current?.schema ?? "") : "",
      server: current?.server ?? "",
      rowLimit: current?.rowLimit ?? 1000,
      ...init,
    });
    tabsNow.current = { tabs: [...now.tabs, tab], active: tab.id };
    setTabs((s) => (s.tabs.length >= maxTabs ? s : { tabs: [...s.tabs, tab], active: tab.id }));
    return tab.id;
  }, []);

  const closeTab = useCallback(
    (id: string) => {
      // What the tab holds now: a key pressed straight after typing (Alt+W) can come before the
      // render that brings the text into `tabs`, and the tab would close without asking.
      const found = tabsNow.current.tabs.find((t) => t.id === id);
      if (!found) return;
      const tab = { ...found, sql: handles.current.get(id)?.text() ?? found.sql };
      const status = statuses[id];
      const why =
        status && status.transaction !== "none"
          ? "This tab has an open transaction. Closing it rolls the transaction back."
          : hasUnsavedText(tab)
            ? `"${tab.title}" has text you haven't saved.`
            : undefined;
      const close = () => {
        handles.current.delete(id);
        setStatuses(({ [id]: _gone, ...rest }) => rest);
        setTabs((s) => {
          const index = s.tabs.findIndex((t) => t.id === id);
          let rest = s.tabs.filter((t) => t.id !== id);
          if (rest.length === 0) rest = [makeTab([])];
          const nextActive = s.active === id ? rest[Math.max(0, index - 1)]!.id : s.active;
          return { tabs: rest, active: nextActive };
        });
      };
      if (!why) {
        close();
        return;
      }
      void confirmDialog(why, { title: `Close "${tab.title}" anyway?`, okLabel: "Close tab", danger: true }).then(
        (ok) => ok && close(),
      );
    },
    [statuses],
  );

  // Requests from elsewhere (the schema browser's preview): a new tab, or the active one.
  const open = useCallback(
    (r: QueryRequest) => {
      if (r.saved) {
        // A saved query: its tab if one has it, else a new tab.
        const savedId = r.saved.id;
        const has = tabsNow.current.tabs.find((t) => t.savedQueryId === savedId);
        if (has) {
          setTabs((st) => ({ ...st, active: has.id }));
          return;
        }
      }
      // A request for another server than the active tab's opens a tab there.
      const activeServer = tabsNow.current.tabs.find((t) => t.id === tabsNow.current.active)?.server ?? "";
      if (r.newTab || (r.server !== undefined && r.server !== activeServer)) {
        const id = addTab({
          ...(r.saved ? { savedQueryId: r.saved.id, savedSql: r.saved.sql } : {}),
          sql: r.sql,
          ...(r.title ? { title: r.title } : {}),
          ...(r.catalog ? { catalog: r.catalog, schema: r.schema ?? "" } : {}),
          ...(r.server !== undefined ? { server: r.server } : {}),
        });
        if (id && r.run) setRunOnOpen((s) => new Set(s).add(id));
      } else {
        const id = activeRef.current;
        patchTab(id, { sql: r.sql });
        if (r.run) setRunOnOpen((s) => new Set(s).add(id));
      }
    },
    [addTab, patchTab],
  );

  // ── Saved queries ─────────────────────────────────────────────────────────────────────────
  const store = useMemo(() => savedQueryStore(user), [user]);
  const [saving, setSaving] = useState<{ tabId: string; as: boolean; items: SavedItem[]; sql: string } | undefined>();
  const save = useCallback(
    async (tabId: string, as: boolean, sql: string) => {
      const found = tabsNow.current.tabs.find((t) => t.id === tabId);
      if (!found) return;
      // The editor's text as it is now: the tab's copy follows it a render later.
      const tab = { ...found, sql };
      let items: SavedItem[];
      try {
        items = await store.list();
      } catch {
        await notifyDialog(
          "This browser doesn't let the page keep saved queries (a private window, or blocked site data).",
          "Saved queries aren't available",
        );
        return;
      }
      const existing = tab.savedQueryId
        ? items.find((i) => i.id === tab.savedQueryId && i.kind === "query")
        : undefined;
      if (existing && !as) {
        await store.saveQuery({
          id: existing.id,
          name: existing.name,
          parentId: existing.parentId,
          sql: tab.sql,
          catalog: tab.catalog,
          schema: tab.schema,
        });
        patchTab(tab.id, { savedSql: tab.sql });
        savedQueriesChanged();
        return;
      }
      setSaving({ tabId, as, items, sql });
    },
    [store, patchTab, setSaving],
  );

  // A saved query renamed or deleted (here, or in another tab of the browser): its tabs follow.
  useEffect(
    () =>
      onSavedQueriesChanged(() => {
        store.list().then(
          (items) =>
            setTabs((st) => {
              let changed = false;
              const next = st.tabs.map((t) => {
                if (!t.savedQueryId) return t;
                const q = items.find((i) => i.id === t.savedQueryId);
                if (!q) {
                  changed = true;
                  const { savedQueryId: _id, savedSql: _sql, ...rest } = t;
                  return rest;
                }
                if (q.name !== t.title) {
                  changed = true;
                  return { ...t, title: q.name };
                }
                return t;
              });
              return changed ? { ...st, tabs: next } : st;
            }),
          () => {},
        );
      }),
    [store],
  );

  const statusesRef = useRef(statuses);
  useEffect(() => {
    statusesRef.current = statuses;
  }, [statuses]);

  useEffect(
    () =>
      session.registerQueryPage({
        open,
        insert: (text) => handles.current.get(activeRef.current)?.insert(text),
        pointTabAt: (catalog, schema, server) => {
          const id = activeRef.current;
          if ((statusesRef.current[id]?.transaction ?? "none") !== "none") {
            return "The active tab has an open transaction: commit or roll it back first.";
          }
          const tab = tabsNow.current.tabs.find((t) => t.id === id);
          if (server !== undefined && (tab?.server ?? "") !== server) {
            return "The active tab is on another server: open a tab on this server first (its menu has New tab).";
          }
          patchTab(id, { catalog, schema: schema ?? "" });
          return undefined;
        },
      }),
    [session, open, patchTab],
  );

  // What the schema browser follows: the active tab's catalog, schema and transaction.
  const activeTab = tabs.find((t) => t.id === active);
  const activeStatus = statuses[active];
  useEffect(() => {
    publishActiveTab({
      catalog: activeTab?.catalog || defaults.catalog,
      schema: activeTab?.schema || defaults.schema,
      transaction: activeStatus?.transaction ?? "none",
      role: activeTab?.role ?? "",
      server: activeTab?.server ?? "",
      sessionId: activeStatus?.sessionId ?? "",
      title: activeTab?.title ?? "",
    });
  }, [
    activeTab?.catalog,
    activeTab?.schema,
    activeTab?.role,
    activeTab?.server,
    activeStatus?.transaction,
    activeStatus?.sessionId,
    activeTab?.title,
    defaults,
  ]);
  useEffect(() => () => resetActiveTab(), []);

  const select = (id: string) => setTabs((s) => ({ ...s, active: id }));

  // The tab strip scrolls sideways with no scrollbar of its own: WebKit's overlay scrollbar
  // sat over the bottom of the tabs and took their clicks. A vertical wheel scrolls it (a
  // trackpad's sideways swipe already does), and the shown tab is always scrolled into view.
  const strip = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = strip.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (e.ctrlKey || Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
      if (el.scrollWidth <= el.clientWidth) return;
      e.preventDefault();
      el.scrollLeft += e.deltaMode === WheelEvent.DOM_DELTA_LINE ? e.deltaY * 16 : e.deltaY;
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);
  useEffect(() => {
    const el = strip.current;
    const tab = el?.querySelector<HTMLElement>('[aria-selected="true"]')?.parentElement;
    if (!el || !tab) return;
    // Only the strip scrolls (scrollIntoView could scroll the page or the panes too).
    const left = tab.offsetLeft - el.offsetLeft;
    const right = left + tab.offsetWidth;
    if (left < el.scrollLeft) el.scrollLeft = left - 8;
    else if (right > el.scrollLeft + el.clientWidth) el.scrollLeft = right - el.clientWidth + 8;
  }, [active, tabs.length]);

  const move = (from: string, to: string) =>
    setTabs((s) => {
      if (from === to) return s;
      const list = [...s.tabs];
      const a = list.findIndex((t) => t.id === from);
      const b = list.findIndex((t) => t.id === to);
      if (a < 0 || b < 0) return s;
      const [moved] = list.splice(a, 1);
      list.splice(b, 0, moved!);
      return { ...s, tabs: list };
    });

  // The save dialog is gone: the cursor goes back to the tab's editor (the dialog's own
  // element, which held it, no longer exists), so typing and the shortcuts carry on.
  const focusAfterSave = useRef<string | undefined>(undefined);
  const endSaving = (tabId: string) => {
    focusAfterSave.current = tabId;
    setSaving(undefined);
  };
  useEffect(() => {
    if (saving || !focusAfterSave.current) return;
    handles.current.get(focusAfterSave.current)?.focus();
    focusAfterSave.current = undefined;
  }, [saving]);

  return (
    <div className="query-page">
      {!noteHidden && (
        <div className="narrow-note" role="note" data-testid="narrow-note">
          <span>The editor works on a phone, but it is easier to use on a larger screen.</span>
          <button
            type="button"
            className="btn btn-ghost btn-icon btn-sm"
            onClick={hideNote}
            aria-label="Hide this note"
          >
            <X size={14} />
          </button>
        </div>
      )}
      <div className="tabs-bar">
        <div className="tabs" role="tablist" aria-label="Editor tabs" ref={strip}>
          {tabs.map((t) => {
            const status = statuses[t.id];
            const marker = status?.running
              ? "running"
              : status?.failed
                ? "failed"
                : status && status.transaction !== "none"
                  ? "transaction"
                  : undefined;
            const unsaved = !!t.savedQueryId && hasUnsavedText(t);
            return (
              <div
                key={t.id}
                className={`tab${t.id === active ? " tab-active" : ""}`}
                draggable={renaming !== t.id}
                data-testid="editor-tab-button"
                onDragStart={(e) => {
                  dragged.current = t.id;
                  e.dataTransfer.effectAllowed = "move";
                  e.dataTransfer.setData("application/x-hardcharger-tab", t.id);
                }}
                onDragOver={(e) => {
                  if (dragged.current) e.preventDefault();
                }}
                onDrop={(e) => {
                  e.preventDefault();
                  if (dragged.current) move(dragged.current, t.id);
                  dragged.current = undefined;
                }}
                onDragEnd={() => (dragged.current = undefined)}
              >
                {renaming === t.id ? (
                  <input
                    className="tab-rename"
                    aria-label="Tab name"
                    defaultValue={t.title}
                    autoFocus
                    onFocus={(e) => e.currentTarget.select()}
                    onBlur={(e) => {
                      const title = e.currentTarget.value.trim();
                      if (title) patchTab(t.id, { title });
                      setRenaming(undefined);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") e.currentTarget.blur();
                      if (e.key === "Escape") setRenaming(undefined);
                    }}
                  />
                ) : (
                  <button
                    type="button"
                    role="tab"
                    aria-selected={t.id === active}
                    className="tab-title"
                    onClick={() => select(t.id)}
                    onDoubleClick={() => setRenaming(t.id)}
                    title={`${t.title}: double-click to rename, drag to move`}
                  >
                    {marker && <span className={`tab-marker tab-marker-${marker}`} data-marker={marker} aria-hidden />}
                    {t.title}
                    {t.server && (
                      <span
                        className="tab-server"
                        title={`On ${serverName(t.server) ?? "another server"}`}
                        data-server={t.server}
                      >
                        {serverName(t.server)}
                      </span>
                    )}
                    {unsaved && (
                      <span className="tab-unsaved" title="Unsaved changes" aria-label="unsaved changes">
                        {" "}
                        ●
                      </span>
                    )}
                  </button>
                )}
                <button
                  type="button"
                  className="tab-close"
                  aria-label={`Close ${t.title}`}
                  title="Close (Alt+W)"
                  onClick={() => closeTab(t.id)}
                >
                  <X size={12} />
                </button>
              </div>
            );
          })}
        </div>
        <div className="tabs-actions">
          <button
            type="button"
            className="btn btn-ghost btn-icon btn-sm"
            onClick={() => addTab()}
            disabled={tabs.length >= maxTabs}
            aria-label="New tab"
            title={tabs.length >= maxTabs ? `At most ${maxTabs} tabs` : "New tab (Alt+N)"}
          >
            <Plus size={14} />
          </button>
          {(servers.servers.some((s) => s.status === "connected") || !session.localServer) && (
            <button
              type="button"
              className="btn btn-ghost btn-icon btn-sm"
              aria-label="New tab on a server"
              title="New tab on… (pick the server)"
              data-testid="new-tab-on"
              onClick={(e) => {
                const r = e.currentTarget.getBoundingClientRect();
                setServerMenu({ x: r.left, y: r.bottom });
              }}
            >
              <ChevronDown size={14} />
            </button>
          )}
          <button
            type="button"
            className="btn btn-ghost btn-icon btn-sm tabs-help"
            onClick={() => setHelp(true)}
            aria-label="How the editor works"
            title="How the editor works: sessions, transactions, statements, results, and what this browser keeps"
          >
            <HelpCircle size={14} />
          </button>
        </div>
      </div>
      {help && <HelpDialog idleTimeoutS={idleTimeoutS} onClose={() => setHelp(false)} />}
      {serverMenu && (
        <FloatingMenu
          x={serverMenu.x}
          y={serverMenu.y}
          items={[
            { label: "New tab on", heading: true },
            ...(session.localServer ? [{ label: "This server", run: () => addTab({ server: localServerId }) }] : []),
            ...servers.servers
              .filter((s) => s.status === "connected")
              .map((s): MenuItem => ({ label: serverLabel(s), run: () => addTab({ server: s.saved.id }) })),
          ]}
          onClose={() => setServerMenu(null)}
        />
      )}

      {tabs.map((t) => (
        <EditorTab
          key={t.id}
          tab={t}
          active={t.id === active}
          theme={theme}
          editorHeight={editorHeight}
          defaults={defaults}
          catalogVersion={catalogVersion}
          onCatalogChanged={() => clearCatalogCaches(session)}
          onChange={(patch) => patchTab(t.id, patch)}
          onStatus={(status) =>
            setStatuses((s) => {
              const old = s[t.id];
              if (
                old &&
                old.running === status.running &&
                old.failed === status.failed &&
                old.transaction === status.transaction &&
                old.sessionId === status.sessionId
              ) {
                return s;
              }
              return { ...s, [t.id]: status };
            })
          }
          onNewTab={() => addTab()}
          onSave={(sql) => void save(t.id, false, sql)}
          onSaveAs={(sql) => void save(t.id, true, sql)}
          roles={t.server ? [] : roles}
          {...(t.server ? { serverName: serverName(t.server) ?? "a removed server" } : {})}
          onCloseTab={() => closeTab(t.id)}
          onReady={(h) => {
            if (h) handles.current.set(t.id, h);
            else handles.current.delete(t.id);
          }}
          runOnOpen={runOnOpen.has(t.id)}
          onRunStarted={() =>
            setRunOnOpen((s) => {
              const next = new Set(s);
              next.delete(t.id);
              return next;
            })
          }
          resizeHandle={
            <ResizeHandle
              direction="row"
              label="Resize the SQL editor"
              sizePx={editorHeight}
              minPx={editorSize.minPx}
              maxPx={editorSize.maxPx()}
              onResize={setEditorHeight}
              onReset={resetEditorHeight}
              testId="editor-resize"
            />
          }
        />
      ))}
      {saving && (
        <SaveQueryDialog
          title={saving.as ? "Save as" : "Save query"}
          initialName={(() => {
            const tab = tabs.find((t) => t.id === saving.tabId);
            return saving.as ? `${tab?.title ?? "Query"} copy` : (tab?.title ?? "Query");
          })()}
          initialFolder={getSelectedFolder()}
          items={saving.items}
          onClose={() => endSaving(saving.tabId)}
          onSave={async (name, folder) => {
            const tab = tabsNow.current.tabs.find((t) => t.id === saving.tabId);
            if (!tab) return;
            const q = await store.saveQuery({
              name,
              parentId: folder,
              sql: saving.sql,
              catalog: tab.catalog,
              schema: tab.schema,
            });
            patchTab(tab.id, { savedQueryId: q.id, savedSql: saving.sql, title: q.name });
            endSaving(tab.id);
            savedQueriesChanged();
          }}
        />
      )}
    </div>
  );
}
