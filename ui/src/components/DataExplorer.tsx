import {
  Binary,
  Braces,
  Calendar,
  ChevronDown,
  ChevronRight,
  CircleDot,
  Columns3,
  Database,
  Eye,
  FileUp,
  FolderTree,
  Hash,
  Layers,
  MoreHorizontal,
  RefreshCw,
  Search,
  Table2,
  ToggleLeft,
  Type,
  X,
} from "lucide-react";
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";

import { currentActiveTab, subscribeActiveTab, useActiveTab } from "../lib/activeTab";
import type { CatalogCache, ColumnEntry, SearchHit, TableEntry } from "../lib/catalogCache";
import { searchMinChars } from "../lib/catalogCache";
import { onCatalogsChanged } from "../lib/catalogs";
import { copyText } from "../lib/clipboard";
import { sqlIdentifier } from "../lib/completionContext";
import { sqlNameDragType } from "../lib/sqlNameDrag";
import { requestUpload } from "../lib/upload/request";
import { errorMessage, useSession } from "../session";
import { FloatingMenu, type MenuItem } from "./FloatingMenu";

type Load<T> = { state: "loading" } | { state: "error"; message: string } | { state: "done"; items: T };

/** Node keys: the path, JSON-encoded so no name can collide with a separator. */
const keyOf = (...path: string[]) => JSON.stringify(path);
/** Catalogs the browser leaves out: `temp` belongs to one session, and the browser isn't a tab's session. */
const hidden = new Set(["temp"]);
const previewRows = 100;
const searchDelayMs = 300;

/** An icon for a DuckDB column type. */
function columnIcon(type: string) {
  const t = type.toUpperCase();
  if (/\[\]$|^(STRUCT|MAP|LIST|UNION|ARRAY|JSON)/.test(t)) return Braces;
  if (
    /^(TINYINT|SMALLINT|INTEGER|INT|BIGINT|HUGEINT|UTINYINT|USMALLINT|UINTEGER|UBIGINT|UHUGEINT|FLOAT|REAL|DOUBLE|DECIMAL|NUMERIC)/.test(
      t,
    )
  ) {
    return Hash;
  }
  if (/^(VARCHAR|TEXT|STRING|CHAR|BPCHAR|UUID|ENUM)/.test(t)) return Type;
  if (/^(DATE|TIME|TIMESTAMP|INTERVAL)/.test(t)) return Calendar;
  if (/^BOOL/.test(t)) return ToggleLeft;
  if (/^(BLOB|BYTEA|BIT|VARBINARY)/.test(t)) return Binary;
  return CircleDot;
}

interface Props {
  /** The listings the active tab's role sees. */
  cache: CatalogCache;
}

/**
 * The schema browser: the catalogs the user may see, their schemas, then Tables and Views, and
 * each one's columns with their types, each level listed when it is opened (Warp SQL metadata,
 * as the active tab's role sees it). Double-click or drag a name into the editor; the menu (right
 * click or …) previews a table in a new tab, copies or inserts names, shows a view's definition,
 * and points the active tab at a catalog and schema. Search finds tables and views anywhere.
 */
export function DataExplorer({ cache }: Props) {
  const session = useSession();
  const active = useActiveTab();
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [loads, setLoads] = useState<Map<string, Load<unknown>>>(new Map());
  const [keywords, setKeywords] = useState<Set<string>>(new Set());
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuItem[] } | null>(null);
  const closeMenu = useCallback(() => setMenu(null), []);
  const [definition, setDefinition] = useState<{ name: string; sql?: string; error?: string } | null>(null);
  const [found, setFound] = useState<string | undefined>();
  const [term, setTerm] = useState("");
  const [searchResult, setSearchResult] = useState<
    { term: string; load: Load<{ hits: SearchHit[]; more: boolean }> } | undefined
  >();
  const [notice, setNotice] = useState<string | undefined>();
  const tree = useRef<HTMLUListElement>(null);
  const [autoOpen] = useState(() => new Once());

  // Names that need quotes (mixed case, spaces, keywords) are written quoted.
  useEffect(() => {
    let live = true;
    cache.vocabulary().then(
      (v) => live && setKeywords(new Set(v.keywords.map((k) => k.toUpperCase()))),
      () => {},
    );
    return () => {
      live = false;
    };
  }, [cache]);
  const ident = useCallback((name: string) => sqlIdentifier(name, keywords), [keywords]);
  const qualified = useCallback((...parts: string[]) => parts.map(ident).join("."), [ident]);

  const setLoad = (key: string, load: Load<unknown>) => setLoads((m) => new Map(m).set(key, load));

  const load = useCallback(
    (key: string, fetch: () => Promise<unknown>, then?: (items: unknown) => void) => {
      setLoad(key, { state: "loading" });
      fetch().then(
        (items) => {
          setLoad(key, { state: "done", items });
          then?.(items);
        },
        (err: unknown) => {
          if (!session.checkSignIn(err)) setLoad(key, { state: "error", message: errorMessage(err) });
        },
      );
    },
    [session],
  );

  // The first time it can, the browser opens the active tab's catalog.
  const openActive = useCallback(() => {
    const catalog = currentActiveTab().catalog;
    if (!catalog) return;
    cache.catalogs().then(
      (names) => {
        if (!names.includes(catalog) || !autoOpen.take()) return;
        setOpen((o) => new Set(o).add(keyOf(catalog)));
        load(keyOf(catalog), () => cache.schemas(catalog));
      },
      () => {},
    );
  }, [cache, load, autoOpen]);
  useEffect(() => subscribeActiveTab(openActive), [openActive]);

  const loadFor = useCallback(
    (path: string[]) => {
      const key = keyOf(...path);
      if (path.length === 0) load(key, () => cache.catalogs(), openActive);
      else if (path.length === 1) load(key, () => cache.schemas(path[0]!));
      else if (path.length === 2) load(key, () => cache.tables(path[0]!, path[1]!));
      else if (path.length === 3) load(key, () => cache.columns(path[0]!, path[1]!, path[2]!));
    },
    [cache, load, openActive],
  );

  // The catalogs, and everything open again: at the start, for a new role, and after a run
  // that may have created, dropped or attached something.
  const relist = useCallback(
    (paths: Set<string>) => {
      setLoads(new Map());
      loadFor([]);
      for (const key of paths) {
        const path = JSON.parse(key) as string[];
        if (path.length <= 3 && path[0] !== "group") loadFor(path);
      }
    },
    [loadFor],
  );
  const openRef = useRef(open);
  useEffect(() => {
    openRef.current = open;
  }, [open]);
  useEffect(() => relist(openRef.current), [relist]);
  useEffect(() => onCatalogsChanged(() => relist(openRef.current)), [relist]);

  const toggle = (path: string[], group?: string) => {
    const key = group ? keyOf("group", ...path, group) : keyOf(...path);
    const opening = !open.has(key);
    setOpen((o) => {
      const next = new Set(o);
      if (next.has(key)) next.delete(key);
      else {
        next.add(key);
        // A schema opens with its Tables and Views groups open.
        if (!group && path.length === 2) {
          next.add(keyOf("group", path[0]!, path[1]!, "tables"));
          next.add(keyOf("group", path[0]!, path[1]!, "views"));
        }
      }
      return next;
    });
    if (opening && !group && !loads.has(key)) loadFor(path);
  };

  // ── Search ────────────────────────────────────────────────────────────────────────────────
  useEffect(() => {
    const t = term.trim();
    if (t.length < searchMinChars) return;
    let live = true;
    const timer = window.setTimeout(() => {
      cache.search(t).then(
        (r) => live && setSearchResult({ term: t, load: { state: "done", items: r } }),
        (err: unknown) => {
          if (live && !session.checkSignIn(err)) {
            setSearchResult({ term: t, load: { state: "error", message: errorMessage(err) } });
          }
        },
      );
    }, searchDelayMs);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [term, cache, session]);

  const reveal = (hit: SearchHit) => {
    const keys = [
      keyOf(hit.catalog),
      keyOf(hit.catalog, hit.schema),
      keyOf("group", hit.catalog, hit.schema, hit.isView ? "views" : "tables"),
    ];
    setOpen((o) => new Set([...o, ...keys]));
    for (const path of [[hit.catalog], [hit.catalog, hit.schema]]) if (!loads.has(keyOf(...path))) loadFor(path);
    const target = keyOf(hit.catalog, hit.schema, hit.name);
    setFound(target);
    setTerm("");
    // Once drawn, bring it into view.
    const scroll = (tries: number) =>
      requestAnimationFrame(() => {
        const el = tree.current?.querySelector<HTMLElement>(`[data-key="${CSS.escape(target)}"]`);
        if (el) el.scrollIntoView({ block: "nearest" });
        else if (tries > 0) window.setTimeout(() => scroll(tries - 1), 100);
      });
    scroll(30);
  };

  // ── Actions ───────────────────────────────────────────────────────────────────────────────
  const insert = (text: string) => session.insertText(text);
  const useBlocked =
    active.transaction !== "none" ? "Commit or roll back the active tab's transaction first" : undefined;
  const pointTabAt = (catalog: string, schema?: string) => {
    const why = session.pointTabAt(catalog, schema);
    if (why) setNotice(why);
  };
  const copy = (text: string) =>
    void copyText(text).then((ok) => setNotice(ok ? `Copied ${text}` : "The browser refused the copy."));
  const preview = (catalog: string, schema: string, name: string) =>
    session.requestQuery({
      sql: `SELECT * FROM ${qualified(catalog, schema, name)} LIMIT ${previewRows};`,
      run: true,
      newTab: true,
      title: name,
      catalog,
      schema,
    });
  const columnsOf = async (catalog: string, schema: string, name: string) =>
    (await cache.columns(catalog, schema, name)).map((c) => ident(c.name));
  const showDefinition = (catalog: string, schema: string, name: string) => {
    setDefinition({ name: qualified(catalog, schema, name) });
    cache.viewDefinition(catalog, schema, name).then(
      (sql) =>
        setDefinition({
          name: qualified(catalog, schema, name),
          ...(sql ? { sql } : { error: "The server has no definition for this view." }),
        }),
      (err: unknown) => setDefinition({ name: qualified(catalog, schema, name), error: errorMessage(err) }),
    );
  };

  const objectMenu = (catalog: string, schema: string, t: TableEntry): MenuItem[] => {
    const name = qualified(catalog, schema, t.name);
    return [
      { label: "Insert name in editor", run: () => insert(name) },
      { label: "Copy name", run: () => copy(name) },
      { label: "Preview data", run: () => preview(catalog, schema, t.name) },
      {
        label: "Insert column names",
        run: () =>
          void columnsOf(catalog, schema, t.name).then(
            (cols) => insert(cols.join(", ")),
            () => {},
          ),
      },
      { label: "Insert SELECT statement", run: () => insert(`SELECT * FROM ${name} LIMIT ${previewRows};`) },
      ...(t.isView ? [{ label: "Show definition", run: () => showDefinition(catalog, schema, t.name) }] : []),
      { label: "Use this catalog and schema", run: () => pointTabAt(catalog, schema), disabled: useBlocked },
    ];
  };
  const upload = (catalog?: string, schema?: string) =>
    requestUpload({
      catalog: catalog ?? active.catalog,
      schema: schema ?? (catalog === undefined ? active.schema : ""),
      role: active.role,
    });
  const schemaMenu = (catalog: string, schema: string): MenuItem[] => [
    { label: "Insert name in editor", run: () => insert(qualified(catalog, schema)) },
    { label: "Copy name", run: () => copy(qualified(catalog, schema)) },
    { label: "Use this catalog and schema", run: () => pointTabAt(catalog, schema), disabled: useBlocked },
    { label: "Upload data…", run: () => upload(catalog, schema) },
  ];
  const catalogMenu = (catalog: string): MenuItem[] => [
    { label: "Insert name in editor", run: () => insert(ident(catalog)) },
    { label: "Copy name", run: () => copy(ident(catalog)) },
    {
      label: "Use this catalog",
      run: () =>
        void cache.schemas(catalog).then(
          (names) => pointTabAt(catalog, names.includes("main") ? "main" : (names[0] ?? "")),
          (err: unknown) => setNotice(errorMessage(err)),
        ),
      disabled: useBlocked,
    },
    { label: "Upload data…", run: () => upload(catalog) },
  ];
  const columnMenu = (column: ColumnEntry): MenuItem[] => [
    { label: "Insert name in editor", run: () => insert(ident(column.name)) },
    { label: "Copy name", run: () => copy(ident(column.name)) },
  ];

  const openMenu = (e: { clientX: number; clientY: number; preventDefault: () => void }, items: MenuItem[]) => {
    e.preventDefault();
    setMenu({ x: e.clientX, y: e.clientY, items });
  };

  useEffect(() => {
    if (!notice) return;
    const t = window.setTimeout(() => setNotice(undefined), 2500);
    return () => window.clearTimeout(t);
  }, [notice]);

  // ── Rendering ─────────────────────────────────────────────────────────────────────────────
  const status = (l: Load<unknown> | undefined, depth: number, retry: () => void, empty = "Empty") => {
    if (!l || l.state === "loading") {
      return (
        <li className={`tree-note depth-${depth}`} aria-busy="true">
          Loading…
        </li>
      );
    }
    if (l.state === "error") {
      return (
        <li className={`tree-note tree-error depth-${depth}`} role="alert">
          <span>{l.message}</span>{" "}
          <button type="button" className="btn btn-ghost btn-sm" onClick={retry}>
            Retry
          </button>
        </li>
      );
    }
    if (Array.isArray(l.items) && l.items.length === 0) return <li className={`tree-note depth-${depth}`}>{empty}</li>;
    return null;
  };
  const items = (key: string): unknown => {
    const l = loads.get(key);
    return l?.state === "done" ? l.items : undefined;
  };

  const row = (options: {
    key: string;
    depth: number;
    label: string;
    icon: ReactNode;
    expandable?: boolean;
    expanded?: boolean;
    onToggle?: () => void;
    drag?: string;
    detail?: ReactNode;
    menu?: MenuItem[];
    title?: string;
  }) => {
    const { key, depth, label, icon, expandable, expanded, onToggle, drag, detail, menu: m, title } = options;
    return (
      <div
        className={`tree-row-wrap${found === key ? " tree-found" : ""}`}
        data-key={key}
        onContextMenu={m ? (e) => openMenu(e, m) : undefined}
      >
        <button
          type="button"
          className={`tree-row depth-${depth}`}
          onClick={onToggle}
          onDoubleClick={
            drag
              ? (e) => {
                  e.preventDefault();
                  insert(drag);
                }
              : undefined
          }
          draggable={!!drag}
          onDragStart={
            drag
              ? (e) => {
                  e.dataTransfer.effectAllowed = "copy";
                  e.dataTransfer.setData("text/plain", drag);
                  e.dataTransfer.setData(sqlNameDragType, drag);
                }
              : undefined
          }
          title={title ?? (drag ? `Double-click or drag to insert ${drag}` : undefined)}
        >
          {expandable ? (
            expanded ? (
              <ChevronDown size={14} />
            ) : (
              <ChevronRight size={14} />
            )
          ) : (
            <span className="tree-spacer" />
          )}
          {icon}
          <span className="tree-name">{label}</span>
          {detail}
        </button>
        {m && (
          <button
            type="button"
            className="btn btn-ghost btn-icon btn-sm tree-action"
            aria-label={`More for ${label}`}
            title="More"
            onClick={(e) => {
              const r = e.currentTarget.getBoundingClientRect();
              openMenu({ clientX: r.left, clientY: r.bottom, preventDefault: () => {} }, m);
            }}
          >
            <MoreHorizontal size={14} />
          </button>
        )}
      </div>
    );
  };

  const objects = (catalog: string, schema: string, list: TableEntry[], kind: "tables" | "views") => {
    const groupKey = keyOf("group", catalog, schema, kind);
    const groupOpen = open.has(groupKey);
    const label = kind === "tables" ? "Tables" : "Views";
    return (
      <li key={groupKey} role="treeitem" aria-expanded={groupOpen} aria-label={label}>
        {row({
          key: groupKey,
          depth: 2,
          label: `${label} (${list.length.toLocaleString()})`,
          icon: <Layers size={14} className="tree-icon" />,
          expandable: true,
          expanded: groupOpen,
          onToggle: () => toggle([catalog, schema], kind),
        })}
        {groupOpen && (
          <ul role="group">
            {list.map((t) => {
              const tk = keyOf(catalog, schema, t.name);
              const tOpen = open.has(tk);
              const cols = items(tk) as ColumnEntry[] | undefined;
              const name = qualified(catalog, schema, t.name);
              return (
                <li key={tk} role="treeitem" aria-expanded={tOpen} aria-label={t.name} data-table-type={t.type}>
                  {row({
                    key: tk,
                    depth: 3,
                    label: t.name,
                    icon: t.isView ? (
                      <Eye size={14} className="tree-icon" />
                    ) : (
                      <Table2 size={14} className="tree-icon" />
                    ),
                    expandable: true,
                    expanded: tOpen,
                    onToggle: () => toggle([catalog, schema, t.name]),
                    drag: name,
                    menu: objectMenu(catalog, schema, t),
                  })}
                  {tOpen && (
                    <ul role="group">
                      {cols?.map((c) => {
                        const Icon = columnIcon(c.type);
                        return (
                          <li key={c.name} role="treeitem" aria-label={c.name}>
                            {row({
                              key: keyOf(catalog, schema, t.name, c.name),
                              depth: 4,
                              label: c.name,
                              icon: <Icon size={13} className="tree-icon" />,
                              drag: ident(c.name),
                              detail: (
                                <span className="tree-type">
                                  {c.type}
                                  {c.nullable ? "" : " NOT NULL"}
                                </span>
                              ),
                              menu: columnMenu(c),
                            })}
                          </li>
                        );
                      })}
                      {status(loads.get(tk), 4, () => loadFor([catalog, schema, t.name]))}
                    </ul>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </li>
    );
  };

  const catalogs = ((items(keyOf()) as string[] | undefined) ?? []).filter((c) => !hidden.has(c));
  const searching = term.trim().length >= searchMinChars;
  const results = searchResult && searchResult.term === term.trim() ? searchResult.load : undefined;
  const hits = results?.state === "done" ? results.items : undefined;

  return (
    <div className="schema-browser" data-testid="data-explorer">
      <div className="sidebar-head">
        <FolderTree size={15} />
        <span>Data</span>
        <button
          type="button"
          className="btn btn-ghost btn-icon btn-sm"
          onClick={() => upload()}
          title="Upload data: a CSV, Excel, Parquet, JSON or Arrow file into a table"
          aria-label="Upload data"
          data-testid="explorer-upload"
        >
          <FileUp size={14} />
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-icon btn-sm"
          onClick={() => {
            cache.clear();
            relist(open);
          }}
          title="Refresh"
          aria-label="Refresh the schema browser"
        >
          <RefreshCw size={14} />
        </button>
      </div>
      <div className="tree-search">
        <Search size={13} aria-hidden />
        <input
          type="text"
          placeholder="Search tables and views…"
          aria-label="Search tables and views"
          value={term}
          onChange={(e) => setTerm(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") setTerm("");
          }}
        />
        {term && (
          <button type="button" className="grid-filter-clear" aria-label="Clear the search" onClick={() => setTerm("")}>
            <X size={12} />
          </button>
        )}
      </div>
      {notice && (
        <p className="tree-notice" role="status">
          {notice}
        </p>
      )}
      {searching && (
        <ul className="search-results" aria-label="Search results" data-testid="search-results">
          {results?.state === "loading" || !results ? <li className="tree-note">Searching…</li> : null}
          {results?.state === "error" && (
            <li className="tree-note tree-error" role="alert">
              {results.message}
            </li>
          )}
          {hits?.hits.length === 0 && <li className="tree-note">No tables or views match.</li>}
          {hits?.hits.map((h) => (
            <li key={keyOf(h.catalog, h.schema, h.name)}>
              <button type="button" className="search-hit" onClick={() => reveal(h)} title="Show it in the tree">
                {h.isView ? <Eye size={13} className="tree-icon" /> : <Table2 size={13} className="tree-icon" />}
                <span className="tree-name">{h.name}</span>
                <span className="tree-type">
                  {h.catalog}.{h.schema}
                </span>
              </button>
            </li>
          ))}
          {hits?.more && <li className="tree-note">More than 100 match: type more of the name.</li>}
        </ul>
      )}
      <ul className="tree" role="tree" aria-label="Catalogs" ref={tree}>
        {catalogs.map((catalog) => {
          const ck = keyOf(catalog);
          const cOpen = open.has(ck);
          return (
            <li key={ck} role="treeitem" aria-expanded={cOpen} aria-label={catalog}>
              {row({
                key: ck,
                depth: 0,
                label: catalog,
                icon: <Database size={14} className="tree-icon" />,
                expandable: true,
                expanded: cOpen,
                onToggle: () => toggle([catalog]),
                drag: ident(catalog),
                menu: catalogMenu(catalog),
              })}
              {cOpen && (
                <ul role="group">
                  {(items(ck) as string[] | undefined)?.map((schema) => {
                    const sk = keyOf(catalog, schema);
                    const sOpen = open.has(sk);
                    const list = items(sk) as TableEntry[] | undefined;
                    return (
                      <li key={sk} role="treeitem" aria-expanded={sOpen} aria-label={schema}>
                        {row({
                          key: sk,
                          depth: 1,
                          label: schema,
                          icon: <FolderTree size={14} className="tree-icon" />,
                          expandable: true,
                          expanded: sOpen,
                          onToggle: () => toggle([catalog, schema]),
                          drag: qualified(catalog, schema),
                          menu: schemaMenu(catalog, schema),
                        })}
                        {sOpen && (
                          <ul role="group">
                            {list &&
                              objects(
                                catalog,
                                schema,
                                list.filter((t) => !t.isView),
                                "tables",
                              )}
                            {list &&
                              objects(
                                catalog,
                                schema,
                                list.filter((t) => t.isView),
                                "views",
                              )}
                            {status(loads.get(sk), 2, () => loadFor([catalog, schema]), "No tables or views")}
                          </ul>
                        )}
                      </li>
                    );
                  })}
                  {status(loads.get(ck), 1, () => loadFor([catalog]), "No schemas")}
                </ul>
              )}
            </li>
          );
        })}
        {status(loads.get(keyOf()), 0, () => loadFor([]), "No catalogs")}
      </ul>

      {menu && <FloatingMenu x={menu.x} y={menu.y} items={menu.items} onClose={closeMenu} />}
      {definition && (
        <DefinitionDialog
          name={definition.name}
          sql={definition.sql}
          error={definition.error}
          onOpen={(sql) => {
            setDefinition(null);
            session.requestQuery({ sql, run: false, newTab: true, title: definition.name });
          }}
          onClose={() => setDefinition(null)}
        />
      )}
    </div>
  );
}

function DefinitionDialog(props: {
  name: string;
  sql: string | undefined;
  error: string | undefined;
  onOpen: (sql: string) => void;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="dialog dialog-wide"
      onClose={props.onClose}
      aria-label={`Definition of ${props.name}`}
    >
      <div className="dialog-head">
        <h2 className="mono">{props.name}</h2>
        <button type="button" className="btn btn-ghost btn-icon" onClick={props.onClose} aria-label="Close">
          <X size={16} />
        </button>
      </div>
      <p className="inspector-meta">
        The view as the server keeps it: its formatting may differ from what was first written.
      </p>
      <pre className="value-text" data-testid="view-definition">
        {props.sql ?? props.error ?? "Loading…"}
      </pre>
      <div className="dialog-foot">
        <button
          type="button"
          className="btn btn-secondary"
          disabled={!props.sql}
          onClick={() => void copyText(props.sql ?? "").then(setCopied)}
        >
          {copied ? "Copied" : "Copy"}
        </button>
        <button
          type="button"
          className="btn btn-primary"
          disabled={!props.sql}
          onClick={() => props.sql && props.onOpen(props.sql)}
        >
          <Columns3 size={14} /> Open in a new tab
        </button>
      </div>
    </dialog>
  );
}

/** Something that happens once: take() is true the first time only. */
class Once {
  #taken = false;
  take(): boolean {
    if (this.#taken) return false;
    this.#taken = true;
    return true;
  }
}
