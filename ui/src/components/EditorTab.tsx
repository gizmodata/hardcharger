import type { TransactionState } from "@gizmodata/warp-sql";
import { Check, Copy, Info, ListOrdered, Play, RotateCcw, Save, Square, Undo2 } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { listingsFor } from "../lib/catalogs";
import { copyText } from "../lib/clipboard";
import type { monaco } from "../lib/monaco";
import { type ScriptRun, cancelNote, messageFor, runScript } from "../lib/script";
import { isTransactionStatement, mayChangeCatalog, splitStatements, statementPreview } from "../lib/sql";
import { type TabDoc, rowLimits } from "../lib/tabs";
import { type SessionInfo, TabSession, TransactionLostError, type Where } from "../lib/tabSession";
import type { ThemeName } from "../lib/theme";
import { errorMessage, useSession } from "../session";
import { ResultView } from "./ResultView";
import { type EditorApi, SqlEditor } from "./SqlEditor";

export interface TabStatus {
  running: boolean;
  failed: boolean;
  transaction: TransactionState;
}

/** What the query page can do to a tab: put text at its cursor, run SQL in it. */
export interface TabHandle {
  insert: (text: string) => void;
  focus: () => void;
}

type Run =
  | { kind: "idle" }
  | { kind: "running"; startedAt: number; statement: number; total: number; rows: number; cancelling: boolean }
  | { kind: "done"; run: ScriptRun; startedAt: number };

interface Props {
  tab: TabDoc;
  active: boolean;
  theme: ThemeName;
  editorHeight: number;
  /** The user's own catalog and schema, for a tab that picked none. */
  defaults: Where;
  onChange: (patch: Partial<TabDoc>) => void;
  onStatus: (status: TabStatus) => void;
  onNewTab: () => void;
  onCloseTab: () => void;
  /** Ctrl/⌘+S and Save: the tab's saved query (asking a name the first time). */
  onSave?: (sql: string) => void;
  /** Save as…: a copy under a new name. */
  onSaveAs?: (sql: string) => void;
  /** The roles the user may act with (Run as); empty: no picker. */
  roles: readonly string[];
  onReady: (handle: TabHandle | undefined) => void;
  /** Run the whole text once the editor is ready (a table preview), then call onRunStarted. */
  runOnOpen?: boolean;
  onRunStarted?: () => void;
  /** Called after a run that may have created, dropped or attached something. */
  onCatalogChanged?: () => void;
  catalogVersion: number;
  /** The bar under the editor that resizes it (shared by every tab). */
  resizeHandle?: ReactNode;
}

const plural = (n: number, word: string) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;

function duration(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
}

/**
 * One editor tab: its editor, toolbar and results, and its own Warp SQL session (TabSession).
 * Tabs stay mounted while hidden, so a run in a background tab keeps going.
 */
export function EditorTab(props: Props) {
  const { tab, active, theme, editorHeight, defaults, onChange, onStatus } = props;
  const app = useSession();
  const tabSession = useMemo(
    () =>
      new TabSession(
        (where) => app.openConnection({ catalog: where.catalog, schema: where.schema, reconnect: false }),
        async (conn) => {
          const row: unknown = (await conn.query("SELECT HARDCHARGER_CURRENT_SESSION() AS s")).get(0)?.toJSON();
          const id = (row as { s?: unknown } | undefined)?.s;
          return typeof id === "string" ? id : undefined;
        },
      ),
    [app],
  );
  const [run, setRun] = useState<Run>({ kind: "idle" });
  // Runs finished in this tab (tests wait on it; data-runs).
  const [finished, setFinished] = useState(0);
  const [shown, setShown] = useState<number | "messages">(0);
  // Expand: the results fill the tab (the editor and its toolbar fold away) until Restore.
  const [expanded, setExpanded] = useState(false);
  const [notice, setNotice] = useState<string | undefined>();
  const [info, setInfo] = useState<SessionInfo | undefined>();
  const [catalogs, setCatalogs] = useState<string[] | undefined>();
  const [schemaList, setSchemaList] = useState<{ catalog: string; names: string[] } | undefined>();
  const [listingError, setListingError] = useState<string | undefined>();
  const editor = useRef<EditorApi | undefined>(undefined);
  const controller = useRef<AbortController | undefined>(undefined);
  const latest = useRef(props);
  useEffect(() => {
    latest.current = props;
  });

  const where: Where = {
    catalog: tab.catalog || defaults.catalog,
    schema: tab.schema || defaults.schema,
    role: tab.role,
  };
  const sessionId = info?.serverId ?? info?.id ?? "";
  const schemas = schemaList && schemaList.catalog === where.catalog ? schemaList.names : undefined;
  const transaction = info?.transaction ?? "none";
  const inTransaction = transaction !== "none";
  const running = run.kind === "running";
  const failed = run.kind === "done" && run.run.statements.some((s) => s.status === "error");

  useEffect(() => {
    onStatus({ running, failed, transaction });
  }, [running, failed, transaction, onStatus]);

  // The tab's session ends with the tab (closing it, or signing out).
  useEffect(
    () => () => {
      controller.current?.abort();
      void tabSession.close();
    },
    [tabSession],
  );

  // The catalogs the user may see, and the schemas of the tab's catalog, for the pickers
  // (Warp SQL metadata, as access control filters it).
  const listings = useMemo(() => listingsFor(app, tab.role), [app, tab.role]);
  useEffect(() => {
    let live = true;
    listings.catalogs().then(
      (names) => {
        if (!live) return;
        setCatalogs(names);
        setListingError(undefined);
      },
      (err: unknown) => {
        if (!live || app.checkSignIn(err)) return;
        setCatalogs([]);
        setListingError(errorMessage(err));
      },
    );
    return () => {
      live = false;
    };
  }, [app, listings, props.catalogVersion]);

  const listSchemas = useCallback((catalog: string) => listings.schemas(catalog), [listings]);

  useEffect(() => {
    let live = true;
    const catalog = where.catalog;
    if (!catalog) return;
    listSchemas(catalog).then(
      (names) => live && setSchemaList({ catalog, names }),
      (err: unknown) => {
        if (!live || app.checkSignIn(err)) return;
        setSchemaList({ catalog, names: [] });
        setListingError(errorMessage(err));
      },
    );
    return () => {
      live = false;
    };
  }, [app, listSchemas, where.catalog, props.catalogVersion]);

  /** After a run: the session's transaction, and its catalog and schema (a USE shows here). */
  const readSession = useCallback(async () => {
    try {
      const next = await tabSession.refresh(where);
      setInfo(next);
      if (next && (next.catalog !== where.catalog || next.schema !== where.schema)) {
        latest.current.onChange({ catalog: next.catalog, schema: next.schema });
      }
    } catch (err) {
      if (app.checkSignIn(err)) return;
      setInfo(undefined);
      if (err instanceof TransactionLostError) setNotice(err.message);
    }
    // where is read when called; the tab's values are current then.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [app, tabSession, where.catalog, where.schema]);

  const execute = useCallback(
    async (statements: string[], range: monaco.IRange | undefined) => {
      if (controller.current || statements.length === 0) return;
      const abort = new AbortController();
      controller.current = abort;
      const startedAt = performance.now();
      setNotice(undefined);
      setRun({ kind: "running", startedAt, statement: 0, total: statements.length, rows: 0, cancelling: false });
      editor.current?.highlight(range);
      const at = { ...where };
      let result: ScriptRun;
      try {
        result = await runScript({
          statements,
          rowLimit: tab.rowLimit,
          signal: abort.signal,
          execute: (sql, signal) => tabSession.stream(at, sql, signal),
          onProgress: (p) => setRun((r) => (r.kind === "running" ? { ...r, statement: p.statement, rows: p.rows } : r)),
          // A transaction statement or a failure changes the transaction: know it before the
          // next statement, in case the server loses the session in between.
          afterStatement: async (s) => {
            setRun((r) => (r.kind === "running" ? { ...r, statement: s.index + 1, rows: 0 } : r));
            if (s.status === "error" || s.status === "cancelled" || isTransactionStatement(s.sql)) {
              setInfo(await tabSession.refresh(at).catch(() => tabSession.info));
            }
          },
        });
      } finally {
        controller.current = undefined;
        editor.current?.highlight(undefined);
      }
      const stopped = result.stoppedAt === undefined ? undefined : result.statements[result.stoppedAt];
      if (stopped?.status === "error" && app.checkSignIn(stopped.error)) return;
      setRun({ kind: "done", run: result, startedAt });
      setShown(result.resultSets.length > 0 ? result.resultSets.length - 1 : 0);
      if (result.resultSets.length === 0 && statements.length > 1) setShown("messages");
      await readSession();
      const notes: string[] = [];
      const replaced = tabSession.takeReplacedNote();
      if (replaced) notes.push(replaced);
      if (stopped?.status === "cancelled") {
        notes.push(cancelNote(result));
        if (tabSession.info?.transaction === "aborted") {
          notes.push("Cancelling aborted the open transaction: run Rollback to continue.");
        }
      }
      if (notes.length) setNotice(notes.join(" "));
      if (mayChangeCatalog(statements.join(";\n"))) latest.current.onCatalogChanged?.();
      setFinished((n) => n + 1);
    },
    // where and tab are read when a run starts.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [app, tabSession, readSession, tab.rowLimit, where.catalog, where.schema],
  );

  const runCurrent = useCallback(() => {
    const current = editor.current?.current();
    if (!current) {
      setNotice("Nothing to run: the cursor is not in a statement.");
      return;
    }
    void execute(current.statements, current.range);
  }, [execute]);

  const runAll = useCallback(() => {
    const all = editor.current?.all();
    if (!all) return;
    void execute(all.statements, all.range);
  }, [execute]);

  const cancel = () => {
    const c = controller.current;
    if (!c) return;
    setRun((r) => (r.kind === "running" ? { ...r, cancelling: true } : r));
    c.abort();
  };

  // A table preview opens with its SELECT and runs it once the editor is there.
  const [editorReady, setEditorReady] = useState(false);
  useEffect(() => {
    if (!props.runOnOpen || !editorReady) return;
    props.onRunStarted?.();
    runAll();
    // fires once per request; onRunStarted clears it
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.runOnOpen, editorReady]);

  useEffect(() => {
    if (active) editor.current?.focus();
  }, [active]);

  const pickCatalog = async (catalog: string) => {
    if (inTransaction) return;
    try {
      const names = await listSchemas(catalog);
      onChange({ catalog, schema: names.includes("main") ? "main" : (names[0] ?? "") });
    } catch (err) {
      if (!app.checkSignIn(err)) setNotice(errorMessage(err));
    }
  };

  const pickSchema = (schema: string) => {
    if (!inTransaction) onChange({ catalog: where.catalog, schema });
  };

  // The tab's catalog and schema (a picker, the schema browser's "Use this catalog") become
  // the open session's at once; a session opens on them.
  useEffect(() => {
    const now = tabSession.info;
    if (
      !tabSession.open ||
      !now ||
      (now.catalog === where.catalog && now.schema === where.schema && now.role === where.role)
    ) {
      return;
    }
    if (now.transaction !== "none") return;
    tabSession.setWhere(where).then(
      () => setInfo(tabSession.info),
      (err: unknown) => {
        if (!app.checkSignIn(err)) setNotice(errorMessage(err));
      },
    );
    // where's parts are the dependencies.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [where.catalog, where.schema, where.role, tabSession, app]);

  const newSession = async () => {
    if (inTransaction && !window.confirm("End this session? The open transaction will be rolled back.")) return;
    await tabSession.close();
    setInfo(undefined);
    setNotice("Session ended. The next run starts a new one.");
  };

  const statementCount = useMemo(() => splitStatements(tab.sql).length, [tab.sql]);
  const done = run.kind === "done" ? run.run : undefined;
  const resultSets = done?.resultSets ?? [];
  const showTabs = !!done && (resultSets.length > 1 || done.statements.length > 1);
  const shownSet = typeof shown === "number" ? resultSets[shown] : undefined;
  const stopped = done?.stoppedAt === undefined ? undefined : done.statements[done.stoppedAt];
  const lockedTitle = inTransaction
    ? "Locked while a transaction is open: commit or roll back to change it"
    : undefined;

  return (
    <div
      className={`editor-tab${expanded ? " editor-tab-expanded" : ""}`}
      hidden={!active}
      data-testid="editor-tab"
      data-tab-id={tab.id}
      data-runs={finished}
      data-running={running || undefined}
    >
      <div className="editor-area" style={{ height: editorHeight }}>
        <SqlEditor
          completion={() => ({
            cache: listings,
            catalog: latest.current.tab.catalog || latest.current.defaults.catalog,
            schema: latest.current.tab.schema || latest.current.defaults.schema,
          })}
          value={tab.sql}
          theme={theme}
          onChange={(sql) => onChange({ sql })}
          onRun={runCurrent}
          onRunAll={runAll}
          onNewTab={props.onNewTab}
          onCloseTab={props.onCloseTab}
          {...(props.onSave ? { onSave: () => props.onSave?.(editor.current?.value() ?? tab.sql) } : {})}
          onReady={(api) => {
            editor.current = api;
            setEditorReady(!!api);
            // The tab on show starts with the cursor in its editor.
            if (api && latest.current.active) requestAnimationFrame(() => api.focus());
            props.onReady(api ? { insert: (text) => api.insert(text), focus: () => api.focus() } : undefined);
          }}
        />
      </div>
      {props.resizeHandle}
      <div className="toolbar">
        {running ? (
          <button
            type="button"
            className="btn btn-danger"
            onClick={cancel}
            disabled={run.cancelling}
            data-testid="cancel-button"
            title="Stop the running statement on the server"
          >
            <Square size={13} /> {run.cancelling ? "Cancelling…" : "Cancel"}
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-primary"
            onClick={runCurrent}
            data-testid="run-button"
            title="Run the statement at the cursor, or the selection (Ctrl/⌘+Enter)"
          >
            <Play size={14} /> Run
          </button>
        )}
        {statementCount > 1 && (
          <button
            type="button"
            className="btn btn-secondary"
            onClick={runAll}
            disabled={running}
            data-testid="run-all-button"
            title="Run every statement in order on this tab's session, stopping at the first error (⇧+Ctrl/⌘+Enter)"
          >
            <ListOrdered size={14} /> Run all ({statementCount.toLocaleString()})
          </button>
        )}
        <label className="toolbar-field" title={lockedTitle}>
          Catalog
          <select
            aria-label="Catalog"
            value={where.catalog}
            disabled={inTransaction || !catalogs}
            onChange={(e) => void pickCatalog(e.target.value)}
          >
            {!catalogs && <option value={where.catalog}>Loading…</option>}
            {catalogs && where.catalog && !catalogs.includes(where.catalog) && (
              <option value={where.catalog}>{where.catalog}</option>
            )}
            {catalogs?.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
        <label className="toolbar-field" title={lockedTitle}>
          Schema
          <select
            aria-label="Schema"
            value={where.schema}
            disabled={inTransaction || !schemas}
            onChange={(e) => pickSchema(e.target.value)}
          >
            {!schemas && <option value={where.schema}>Loading…</option>}
            {schemas && where.schema && !schemas.includes(where.schema) && (
              <option value={where.schema}>{where.schema}</option>
            )}
            {schemas?.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        {props.roles.length > 0 && (
          <label className="toolbar-field" title={lockedTitle ?? "The role this tab acts with (SET ROLE)"}>
            Run as
            <select
              aria-label="Run as"
              value={tab.role}
              disabled={inTransaction}
              onChange={(e) => onChange({ role: e.target.value })}
            >
              <option value="">All my roles</option>
              {tab.role && !props.roles.includes(tab.role) && <option value={tab.role}>{tab.role}</option>}
              {props.roles.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </label>
        )}
        <label className="toolbar-field">
          Row limit
          <select
            aria-label="Row limit"
            value={tab.rowLimit}
            onChange={(e) => onChange({ rowLimit: Number(e.target.value) })}
          >
            {rowLimits.map((n) => (
              <option key={n} value={n}>
                {n.toLocaleString()}
              </option>
            ))}
          </select>
        </label>
        {props.onSave && (
          <span className="button-pair">
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => props.onSave?.(editor.current?.value() ?? tab.sql)}
              title="Save the tab's SQL as a saved query (Ctrl/⌘+S)"
              data-testid="save-button"
            >
              <Save size={14} /> Save
            </button>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => props.onSaveAs?.(editor.current?.value() ?? tab.sql)}
              title="Save a copy under a new name"
              aria-label="Save as…"
            >
              Save as…
            </button>
          </span>
        )}
        <span className="toolbar-spacer" />
        <span className="toolbar-hint">
          Ctrl/⌘+Enter runs the statement at the cursor (or the selection) · ⇧ runs all · Alt+N new tab · Alt+W close
          tab
        </span>
      </div>

      {(info || inTransaction) && (
        <div className="session-bar" data-testid="session-bar">
          {info && (
            <button
              type="button"
              className="chip chip-button mono"
              data-testid="session-chip"
              title={`Session ${sessionId}: the server's log lines, audit records and KILL SESSION name it so (Warp SQL session ${info.id}). Click to copy.`}
              onClick={() =>
                void copyText(sessionId).then((ok) =>
                  setNotice(ok ? "Session id copied." : "The browser refused the copy."),
                )
              }
            >
              Session {sessionId.replace(/^s_/, "").slice(0, 8)} <Copy size={11} />
            </button>
          )}
          {transaction === "open" && (
            <>
              <span className="badge badge-warn" data-testid="in-transaction">
                In transaction
              </span>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={running}
                onClick={() => void execute(["COMMIT"], undefined)}
              >
                <Check size={14} /> Commit
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={running}
                onClick={() => void execute(["ROLLBACK"], undefined)}
              >
                <Undo2 size={14} /> Rollback
              </button>
            </>
          )}
          {transaction === "aborted" && (
            <>
              <span className="badge badge-danger" data-testid="transaction-failed">
                Transaction failed — Rollback to continue
              </span>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={running}
                onClick={() => void execute(["ROLLBACK"], undefined)}
              >
                <Undo2 size={14} /> Rollback
              </button>
            </>
          )}
          <span className="toolbar-spacer" />
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            disabled={running}
            onClick={() => void newSession()}
            title="End this tab's session (rolling back an open transaction); the next run starts a new one"
          >
            <RotateCcw size={13} /> New session
          </button>
        </div>
      )}

      {notice && (
        <div className="notice" role="status" data-testid="tab-notice">
          <Info size={14} />
          <span>{notice}</span>
        </div>
      )}
      {listingError && <p className="listing-note">Catalog and schema lists are unavailable: {listingError}</p>}

      <section className="results" aria-label="Query results">
        <div className="results-bar">
          <Status run={run} />
        </div>
        {stopped?.status === "error" && (
          <div className="result-error" role="alert" data-testid="query-error">
            <strong>
              {done && done.statements.length > 1
                ? `Statement ${stopped.index + 1} of ${done.statements.length} failed`
                : errorCode(stopped.error)}
            </strong>
            <pre>{errorMessage(stopped.error)}</pre>
          </div>
        )}
        {showTabs && done && (
          <div className="result-tabs" role="tablist" aria-label="Result sets">
            {resultSets.map((rs, i) => (
              <button
                key={rs.statement}
                type="button"
                role="tab"
                aria-selected={shown === i}
                className={`result-tab${shown === i ? " result-tab-active" : ""}`}
                title={done.statements[rs.statement]?.sql}
                onClick={() => setShown(i)}
              >
                Result {i + 1}
                <span className="result-tab-detail">
                  {" "}
                  · {statementPreview(done.statements[rs.statement]?.sql ?? "", 32)} ({plural(rs.table.numRows, "row")})
                </span>
              </button>
            ))}
            <button
              type="button"
              role="tab"
              aria-selected={shown === "messages"}
              className={`result-tab${shown === "messages" ? " result-tab-active" : ""}`}
              onClick={() => setShown("messages")}
            >
              Messages
            </button>
          </div>
        )}
        <div className="results-body">
          {done && shown === "messages" && showTabs ? (
            <ol className="messages" data-testid="messages">
              {done.statements.map((s) => (
                <li key={s.index} className={`message message-${s.status}`}>
                  <span className="message-n">{s.index + 1}.</span>
                  <span className="message-sql mono" title={s.sql}>
                    {statementPreview(s.sql, 120)}
                  </span>
                  <span className="message-result">
                    {messageFor(s)}
                    {s.status !== "not-run" ? ` · ${duration(s.elapsedMs)}` : ""}
                  </span>
                </li>
              ))}
            </ol>
          ) : shownSet ? (
            <ResultView
              key={`${run.kind === "done" ? run.startedAt : 0}:${shown}`}
              table={shownSet.table}
              truncatedAt={shownSet.truncated ? tab.rowLimit : undefined}
              expanded={expanded}
              onExpand={setExpanded}
            />
          ) : done && done.statements.some((s) => s.omitted) && typeof shown === "number" ? (
            <div className="result-message">
              This result set was too large to keep with the others. Run that statement on its own to see it.
            </div>
          ) : done && stopped === undefined ? (
            <div className="result-message" data-testid="query-message">
              {doneMessage(done)}
            </div>
          ) : run.kind === "idle" ? (
            <div className="result-message result-empty">Run a query to see its results here.</div>
          ) : run.kind === "running" ? (
            <div className="result-message result-empty">Running…</div>
          ) : null}
        </div>
      </section>
    </div>
  );
}

function errorCode(err: unknown): string {
  const code = (err as { code?: unknown } | undefined)?.code;
  return typeof code === "string" ? code.replaceAll("_", " ") : "error";
}

function doneMessage(run: ScriptRun): string {
  const last = run.statements.at(-1);
  if (run.statements.length > 1) return `${plural(run.statements.length, "statement")} ran.`;
  if (last?.status === "update") {
    return last.rowsAffected === null || last.rowsAffected === undefined
      ? "Done."
      : `${plural(last.rowsAffected, "row")} changed.`;
  }
  return "Done.";
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
  if (run.kind === "running") {
    const which = run.total > 1 ? ` statement ${Math.min(run.statement + 1, run.total)} of ${run.total},` : "";
    text = `${run.cancelling ? "Cancelling" : "Running"}…${which} ${duration(Math.max(0, now - run.startedAt))}${run.rows ? `, ${plural(run.rows, "row")}` : ""}`;
  } else if (run.kind === "done") {
    const r = run.run;
    const stopped = r.stoppedAt === undefined ? undefined : r.statements[r.stoppedAt];
    const parts: string[] = [];
    const last = r.resultSets.at(-1);
    if (r.statements.length === 1) {
      const s = r.statements[0]!;
      if (s.status === "rows") parts.push(plural(s.rows ?? 0, "row"));
      else if (s.status === "update" && s.rowsAffected != null) parts.push(`${plural(s.rowsAffected, "row")} affected`);
    } else {
      parts.push(plural(r.statements.filter((s) => s.status !== "not-run").length, "statement"));
      if (last) parts.push(plural(r.resultSets.length, "result set"));
    }
    const verb =
      stopped?.status === "error" ? "Failed after" : stopped?.status === "cancelled" ? "Cancelled after" : "in";
    text = `${parts.join(" · ")}${parts.length ? " " : ""}${verb} ${duration(r.elapsedMs)}`;
  }
  return (
    <span className="results-status" data-testid="results-status" aria-live="polite">
      {text}
    </span>
  );
}
