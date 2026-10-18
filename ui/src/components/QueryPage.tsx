import type { Connection } from "@gizmodata/warp-sql";
import { type RecordBatch, Table } from "apache-arrow";
import { Copy, Download, Play, Plus, Square, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { copyText } from "../lib/clipboard";
import { saveFile, exportName } from "../lib/download";
import { type ExportFormat, exportFormats, exportTable, toDelimited } from "../lib/export";
import type { ThemeName } from "../lib/theme";
import { type QueryRequest, errorMessage, useSession } from "../session";
import { ResultsGrid } from "./ResultsGrid";
import { type EditorApi, SqlEditor } from "./SqlEditor";
import "../styles/query.css";

interface TabDoc {
  id: string;
  title: string;
  sql: string;
}

type Run =
  | { kind: "idle" }
  | { kind: "running"; startedAt: number; rows: number; cancelling: boolean }
  | { kind: "rows"; table: Table; truncated: boolean; limit: number; startedAt: number; elapsedMs: number }
  | { kind: "update"; rowsAffected: number | null; elapsedMs: number }
  | { kind: "error"; message: string; code: string | undefined; elapsedMs: number }
  | { kind: "cancelled"; elapsedMs: number };

const tabsKey = "hardcharger-ui:tabs";
const limitKey = "hardcharger-ui:row-limit";
const rowLimits = [1000, 10_000, 100_000, 1_000_000];

// The editor tabs' SQL is kept in localStorage (it is the user's work, not a credential), so
// it survives a reload and a new sign-in.
function loadTabs(): { tabs: TabDoc[]; active: string } {
  try {
    const raw = window.localStorage.getItem(tabsKey);
    if (raw) {
      const saved = JSON.parse(raw) as { tabs?: TabDoc[]; active?: string };
      const tabs = (saved.tabs ?? []).filter(
        (t) => typeof t.id === "string" && typeof t.sql === "string" && typeof t.title === "string",
      );
      if (tabs.length) return { tabs, active: tabs.some((t) => t.id === saved.active) ? saved.active! : tabs[0]!.id };
    }
  } catch {
    // start fresh
  }
  const first = {
    id: newId(),
    title: "Query 1",
    sql: "SELECT HARDCHARGER_VERSION() AS version, HARDCHARGER_USER() AS me;\n",
  };
  return { tabs: [first], active: first.id };
}

function loadLimit(): number {
  try {
    const n = Number(window.localStorage.getItem(limitKey));
    if (rowLimits.includes(n)) return n;
  } catch {
    // default
  }
  return 10_000;
}

function newId(): string {
  return typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `t-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function nextTitle(tabs: TabDoc[]): string {
  let n = tabs.length + 1;
  while (tabs.some((t) => t.title === `Query ${n}`)) n++;
  return `Query ${n}`;
}

const plural = (n: number, word: string) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;

function duration(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
}

interface Props {
  theme: ThemeName;
}

export default function QueryPage({ theme }: Props) {
  const session = useSession();
  const [{ tabs, active }, setTabs] = useState(loadTabs);
  const [runs, setRuns] = useState<Record<string, Run>>({});
  const [rowLimit, setRowLimit] = useState(loadLimit);
  const [notice, setNotice] = useState<string | undefined>();
  const [exportOpen, setExportOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [editorHeight, setEditorHeight] = useState(240);
  const editors = useRef(new Map<string, EditorApi>());
  const connections = useRef(new Map<string, Promise<Connection>>());
  const controllers = useRef(new Map<string, AbortController>());
  const activeRef = useRef(active);

  useEffect(() => {
    activeRef.current = active;
    try {
      window.localStorage.setItem(tabsKey, JSON.stringify({ tabs, active }));
    } catch {
      // not saved
    }
  }, [tabs, active]);

  // Every tab's connection ends with the page (sign-out ends the page too).
  useEffect(() => {
    const open = connections.current;
    const running = controllers.current;
    return () => {
      for (const c of running.values()) c.abort();
      for (const c of open.values()) void c.then((conn) => conn.close()).catch(() => {});
    };
  }, []);

  useEffect(() => {
    if (!notice) return;
    const t = window.setTimeout(() => setNotice(undefined), 2500);
    return () => window.clearTimeout(t);
  }, [notice]);

  const setRun = useCallback((id: string, run: Run) => setRuns((r) => ({ ...r, [id]: run })), []);

  const connectionFor = useCallback(
    (id: string): Promise<Connection> => {
      let c = connections.current.get(id);
      if (!c) {
        c = session.openConnection();
        c.catch(() => connections.current.delete(id));
        connections.current.set(id, c);
      }
      return c;
    },
    [session],
  );

  const run = useCallback(
    async (id: string, sql: string) => {
      if (controllers.current.has(id)) return; // already running
      const controller = new AbortController();
      controllers.current.set(id, controller);
      const startedAt = performance.now();
      const elapsed = () => performance.now() - startedAt;
      setRun(id, { kind: "running", startedAt, rows: 0, cancelling: false });
      try {
        const conn = await connectionFor(id);
        const stream = conn.stream(sql, undefined, { signal: controller.signal });
        const affected = await stream.rowsAffected();
        if (affected !== undefined) {
          setRun(id, { kind: "update", rowsAffected: affected, elapsedMs: elapsed() });
          return;
        }
        const schema = await stream.schema();
        const batches: RecordBatch[] = [];
        let rows = 0;
        let truncated = false;
        let lastUpdate = 0;
        for await (const batch of stream) {
          if (rows + batch.numRows > rowLimit) {
            batches.push(batch.slice(0, rowLimit - rows));
            rows = rowLimit;
            truncated = true;
            break; // closing the stream releases the statement on the server
          }
          batches.push(batch);
          rows += batch.numRows;
          if (performance.now() - lastUpdate > 200) {
            lastUpdate = performance.now();
            setRun(id, { kind: "running", startedAt, rows, cancelling: controller.signal.aborted });
          }
        }
        setRun(id, {
          kind: "rows",
          table: new Table(schema, batches),
          truncated,
          limit: rowLimit,
          startedAt,
          elapsedMs: elapsed(),
        });
      } catch (err) {
        if (controller.signal.aborted) {
          setRun(id, { kind: "cancelled", elapsedMs: elapsed() });
        } else if (!session.checkSignIn(err)) {
          const code = (err as { code?: unknown }).code;
          setRun(id, {
            kind: "error",
            message: errorMessage(err),
            code: typeof code === "string" ? code : undefined,
            elapsedMs: elapsed(),
          });
        }
      } finally {
        controllers.current.delete(id);
        editors.current.get(id)?.highlight(undefined);
      }
    },
    [connectionFor, rowLimit, session, setRun],
  );

  const cancel = useCallback((id: string) => {
    const c = controllers.current.get(id);
    if (!c) return;
    setRuns((r) => {
      const cur = r[id];
      return cur?.kind === "running" ? { ...r, [id]: { ...cur, cancelling: true } } : r;
    });
    c.abort();
  }, []);

  const runCurrent = useCallback(() => {
    const id = activeRef.current;
    const editor = editors.current.get(id);
    const current = editor?.current();
    if (!current) {
      setNotice("Nothing to run: the cursor is not in a statement.");
      return;
    }
    editor?.highlight(current.range);
    void run(id, current.sql);
  }, [run]);

  // Requests from elsewhere (the schema browser's preview): a new tab, or the active one,
  // with the statement, maybe run at once.
  const open = useCallback(
    (r: QueryRequest) => {
      if (r.newTab) {
        const tab = { id: newId(), title: r.title ?? "Query", sql: r.sql };
        setTabs((s) => ({ tabs: [...s.tabs, tab], active: tab.id }));
        if (r.run) void run(tab.id, r.sql);
      } else {
        const id = activeRef.current;
        setTabs((s) => ({ ...s, tabs: s.tabs.map((t) => (t.id === id ? { ...t, sql: r.sql } : t)) }));
        if (r.run) void run(id, r.sql);
      }
    },
    [run],
  );

  useEffect(
    () =>
      session.registerQueryPage({
        open,
        insert: (text) => editors.current.get(activeRef.current)?.insert(text),
      }),
    [session, open],
  );

  const addTab = () =>
    setTabs((s) => {
      const tab = { id: newId(), title: nextTitle(s.tabs), sql: "" };
      return { tabs: [...s.tabs, tab], active: tab.id };
    });

  const closeTab = (id: string) => {
    controllers.current.get(id)?.abort();
    const c = connections.current.get(id);
    connections.current.delete(id);
    void c?.then((conn) => conn.close()).catch(() => {});
    setRuns((r) => {
      const { [id]: _gone, ...rest } = r;
      return rest;
    });
    setTabs((s) => {
      const index = s.tabs.findIndex((t) => t.id === id);
      let rest = s.tabs.filter((t) => t.id !== id);
      if (rest.length === 0) rest = [{ id: newId(), title: "Query 1", sql: "" }];
      const nextActive = s.active === id ? rest[Math.max(0, index - 1)]!.id : s.active;
      return { tabs: rest, active: nextActive };
    });
  };

  const renameTab = (id: string) => {
    const tab = tabs.find((t) => t.id === id);
    const title = window.prompt("Tab name", tab?.title ?? "");
    if (title?.trim())
      setTabs((s) => ({ ...s, tabs: s.tabs.map((t) => (t.id === id ? { ...t, title: title.trim() } : t)) }));
  };

  const current = runs[active] ?? { kind: "idle" };

  const doExport = async (format: ExportFormat) => {
    setExportOpen(false);
    if (current.kind !== "rows") return;
    setExporting(true);
    try {
      const blob = await exportTable(current.table, format);
      saveFile(blob, exportName(exportFormats.find((f) => f.format === format)!.extension));
      setNotice(`Exported ${plural(current.table.numRows, "row")} as ${format.toUpperCase()}.`);
    } catch (err) {
      console.info("export failed:", err);
      setNotice(`The ${format.toUpperCase()} export failed: ${errorMessage(err)}`);
    } finally {
      setExporting(false);
    }
  };

  const copyAll = async () => {
    if (current.kind !== "rows") return;
    const ok = await copyText(toDelimited(current.table, "\t"));
    setNotice(ok ? `Copied ${plural(current.table.numRows, "row")} with headers.` : "The browser refused the copy.");
  };

  const startResize = (e: React.PointerEvent) => {
    e.preventDefault();
    const startY = e.clientY;
    const start = editorHeight;
    const move = (ev: PointerEvent) =>
      setEditorHeight(Math.max(100, Math.min(window.innerHeight - 200, start + ev.clientY - startY)));
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  return (
    <div className="query-page">
      <div className="tabs" role="tablist" aria-label="Editor tabs">
        {tabs.map((t) => (
          <div
            key={t.id}
            className={`tab${t.id === active ? " tab-active" : ""}${runs[t.id]?.kind === "running" ? " tab-running" : ""}`}
          >
            <button
              type="button"
              role="tab"
              aria-selected={t.id === active}
              className="tab-title"
              onClick={() => setTabs((s) => ({ ...s, active: t.id }))}
              onDoubleClick={() => renameTab(t.id)}
              title="Double-click to rename"
            >
              {t.title}
            </button>
            <button
              type="button"
              className="tab-close"
              aria-label={`Close ${t.title}`}
              title="Close"
              onClick={() => closeTab(t.id)}
            >
              <X size={12} />
            </button>
          </div>
        ))}
        <button
          type="button"
          className="btn btn-ghost btn-icon btn-sm"
          onClick={addTab}
          aria-label="New tab"
          title="New tab"
        >
          <Plus size={14} />
        </button>
      </div>

      <div className="editor-area" style={{ height: editorHeight }}>
        {tabs.map((t) => (
          <div key={t.id} className="editor-slot" hidden={t.id !== active}>
            <SqlEditor
              value={t.sql}
              theme={theme}
              onChange={(sql) =>
                setTabs((s) => ({ ...s, tabs: s.tabs.map((x) => (x.id === t.id ? { ...x, sql } : x)) }))
              }
              onRun={runCurrent}
              onReady={(api) => {
                if (api) editors.current.set(t.id, api);
                else editors.current.delete(t.id);
              }}
            />
          </div>
        ))}
      </div>

      <div className="toolbar">
        {current.kind === "running" ? (
          <button
            type="button"
            className="btn btn-danger"
            onClick={() => cancel(active)}
            disabled={current.cancelling}
            data-testid="cancel-button"
          >
            <Square size={13} /> {current.cancelling ? "Cancelling…" : "Cancel"}
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-primary"
            onClick={runCurrent}
            data-testid="run-button"
            title="Run (Ctrl/⌘+Enter)"
          >
            <Play size={14} /> Run
          </button>
        )}
        <span className="toolbar-hint">Ctrl/⌘+Enter runs the statement under the cursor or the selection</span>
        <span className="toolbar-spacer" />
        <label className="toolbar-limit">
          Row limit
          <select
            value={rowLimit}
            onChange={(e) => {
              const n = Number(e.target.value);
              setRowLimit(n);
              try {
                window.localStorage.setItem(limitKey, String(n));
              } catch {
                // not saved
              }
            }}
          >
            {rowLimits.map((n) => (
              <option key={n} value={n}>
                {n.toLocaleString()}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div
        className="split-resizer"
        role="separator"
        aria-orientation="horizontal"
        aria-label="Resize the editor"
        onPointerDown={startResize}
      />

      <section className="results" aria-label="Query results">
        <div className="results-bar">
          <Status key={current.kind === "running" ? current.startedAt : current.kind} run={current} />
          <span className="toolbar-spacer" />
          {notice && (
            <span className="results-notice" role="status" data-testid="results-notice">
              {notice}
            </span>
          )}
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => void copyAll()}
            disabled={current.kind !== "rows"}
            title="Copy every row, with headers, as tab-separated text"
          >
            <Copy size={14} /> Copy
          </button>
          <div className="menu-wrap">
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => setExportOpen((o) => !o)}
              disabled={current.kind !== "rows" || exporting}
              aria-haspopup="menu"
              aria-expanded={exportOpen}
              data-testid="export-button"
            >
              <Download size={14} /> {exporting ? "Exporting…" : "Export"}
            </button>
            {exportOpen && (
              <div className="menu" role="menu" onMouseLeave={() => setExportOpen(false)}>
                {exportFormats.map((f) => (
                  <button key={f.format} type="button" role="menuitem" onClick={() => void doExport(f.format)}>
                    {f.label}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>
        <div className="results-body">
          {current.kind === "rows" && (
            <ResultsGrid
              key={`${active}:${current.startedAt}`}
              table={current.table}
              onCopied={(n) =>
                setNotice(n === false ? "The browser refused the copy." : `Copied ${plural(n, "cell")}.`)
              }
            />
          )}
          {current.kind === "error" && (
            <div className="result-error" role="alert" data-testid="query-error">
              <strong>{current.code ? current.code.replaceAll("_", " ") : "error"}</strong>
              <pre>{current.message}</pre>
            </div>
          )}
          {current.kind === "update" && (
            <div className="result-message" data-testid="query-message">
              {current.rowsAffected === null ? "Done." : `${plural(current.rowsAffected, "row")} changed.`}
            </div>
          )}
          {current.kind === "cancelled" && (
            <div className="result-message" data-testid="query-message">
              Cancelled after {duration(current.elapsedMs)}.
            </div>
          )}
          {current.kind === "idle" && (
            <div className="result-message result-empty">Run a query to see its results here.</div>
          )}
          {current.kind === "running" && <div className="result-message result-empty">Running…</div>}
        </div>
      </section>
    </div>
  );
}

function Status({ run }: { run: Run }) {
  const startedAt = run.kind === "running" ? run.startedAt : 0;
  const [now, setNow] = useState(startedAt);
  useEffect(() => {
    if (run.kind !== "running") return;
    const t = window.setInterval(() => setNow(performance.now()), 100);
    return () => window.clearInterval(t);
  }, [run.kind]);
  let text = "";
  switch (run.kind) {
    case "running":
      text = `${run.cancelling ? "Cancelling" : "Running"}… ${duration(Math.max(0, now - run.startedAt))}${run.rows ? `, ${plural(run.rows, "row")}` : ""}`;
      break;
    case "rows":
      text = `${plural(run.table.numRows, "row")} in ${duration(run.elapsedMs)}${run.truncated ? ` (the first ${run.limit.toLocaleString()}; more were not read)` : ""}`;
      break;
    case "update":
      text = `Done in ${duration(run.elapsedMs)}`;
      break;
    case "error":
      text = `Failed after ${duration(run.elapsedMs)}`;
      break;
    case "cancelled":
      text = "Cancelled";
      break;
    case "idle":
      text = "";
  }
  return (
    <span className="results-status" data-testid="results-status" aria-live="polite">
      {text}
    </span>
  );
}
