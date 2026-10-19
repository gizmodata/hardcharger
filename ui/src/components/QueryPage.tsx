import { Plus, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { useStoredSize } from "../lib/storedSize";
import { type StoredTabs, type TabDoc, hasUnsavedText, loadTabs, makeTab, maxTabs, saveTabs } from "../lib/tabs";
import type { Where } from "../lib/tabSession";
import type { ThemeName } from "../lib/theme";
import { type QueryRequest, useSession } from "../session";
import { EditorTab, type TabHandle, type TabStatus } from "./EditorTab";
import { ResizeHandle } from "./ResizeHandle";
import "../styles/query.css";

const firstSql = "SELECT HARDCHARGER_VERSION() AS version, HARDCHARGER_USER() AS me;\n";
const editorSize = {
  key: "hardcharger-ui:editor-height",
  defaultPx: 240,
  minPx: 100,
  maxPx: () => Math.max(100, Math.round(window.innerHeight * 0.7)),
};

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
 * The query page: editor tabs (up to 20), each with its own Warp SQL session. New with + or
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
  const handles = useRef(new Map<string, TabHandle>());
  const activeRef = useRef(active);
  const dragged = useRef<string | undefined>(undefined);

  useEffect(() => {
    activeRef.current = active;
    saveTabs(user, { tabs, active });
  }, [tabs, active, user]);

  // The user's own catalog and schema: what a tab that picked none starts on.
  useEffect(() => {
    let live = true;
    session.connection
      .getSession()
      .then((doc) => {
        const o = doc.options ?? {};
        if (live) {
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
  }, [session]);

  // Leaving the page with a transaction open loses it: the browser asks first.
  const anyTransaction = Object.values(statuses).some((s) => s.transaction !== "none");
  useEffect(() => {
    if (!anyTransaction) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [anyTransaction]);

  const patchTab = useCallback(
    (id: string, patch: Partial<TabDoc>) =>
      setTabs((s) => ({ ...s, tabs: s.tabs.map((t) => (t.id === id ? { ...t, ...patch } : t)) })),
    [],
  );

  const addTab = useCallback((init: Partial<TabDoc> = {}): string | undefined => {
    let id: string | undefined;
    setTabs((s) => {
      if (s.tabs.length >= maxTabs) return s;
      const current = s.tabs.find((t) => t.id === s.active);
      const tab = makeTab(s.tabs, {
        catalog: current?.catalog ?? "",
        schema: current?.schema ?? "",
        rowLimit: current?.rowLimit ?? 1000,
        ...init,
      });
      id = tab.id;
      return { tabs: [...s.tabs, tab], active: tab.id };
    });
    return id;
  }, []);

  const closeTab = useCallback(
    (id: string) => {
      const tab = tabs.find((t) => t.id === id);
      if (!tab) return;
      const status = statuses[id];
      const why =
        status && status.transaction !== "none"
          ? "This tab has an open transaction. Closing it rolls the transaction back."
          : hasUnsavedText(tab)
            ? `"${tab.title}" has text you haven't saved.`
            : undefined;
      if (why && !window.confirm(`${why} Close it anyway?`)) return;
      handles.current.delete(id);
      setStatuses(({ [id]: _gone, ...rest }) => rest);
      setTabs((s) => {
        const index = s.tabs.findIndex((t) => t.id === id);
        let rest = s.tabs.filter((t) => t.id !== id);
        if (rest.length === 0) rest = [makeTab([])];
        const nextActive = s.active === id ? rest[Math.max(0, index - 1)]!.id : s.active;
        return { tabs: rest, active: nextActive };
      });
    },
    [tabs, statuses],
  );

  // Requests from elsewhere (the schema browser's preview): a new tab, or the active one.
  const open = useCallback(
    (r: QueryRequest) => {
      if (r.newTab) {
        const id = addTab({ sql: r.sql, ...(r.title ? { title: r.title } : {}) });
        if (id && r.run) setRunOnOpen((s) => new Set(s).add(id));
      } else {
        const id = activeRef.current;
        patchTab(id, { sql: r.sql });
        if (r.run) setRunOnOpen((s) => new Set(s).add(id));
      }
    },
    [addTab, patchTab],
  );

  useEffect(
    () =>
      session.registerQueryPage({
        open,
        insert: (text) => handles.current.get(activeRef.current)?.insert(text),
      }),
    [session, open],
  );

  const select = (id: string) => setTabs((s) => ({ ...s, active: id }));

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

  return (
    <div className="query-page">
      <div className="tabs" role="tablist" aria-label="Editor tabs">
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
      </div>

      {tabs.map((t) => (
        <EditorTab
          key={t.id}
          tab={t}
          active={t.id === active}
          theme={theme}
          editorHeight={editorHeight}
          defaults={defaults}
          catalogVersion={catalogVersion}
          onCatalogChanged={() => setCatalogVersion((v) => v + 1)}
          onChange={(patch) => patchTab(t.id, patch)}
          onStatus={(status) =>
            setStatuses((s) => {
              const old = s[t.id];
              if (
                old &&
                old.running === status.running &&
                old.failed === status.failed &&
                old.transaction === status.transaction
              ) {
                return s;
              }
              return { ...s, [t.id]: status };
            })
          }
          onNewTab={() => addTab()}
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
    </div>
  );
}
