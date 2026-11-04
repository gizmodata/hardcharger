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
  Lock,
  LockOpen,
  MoreHorizontal,
  Plus,
  RefreshCw,
  Search,
  Server,
  Table2,
  ToggleLeft,
  Type,
  X,
} from "lucide-react";
import { type CSSProperties, type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { currentActiveTab, subscribeActiveTab, useActiveTab } from "../lib/activeTab";
import type { CatalogCache, ColumnEntry, SearchHit, TableEntry } from "../lib/catalogCache";
import { searchMinChars } from "../lib/catalogCache";
import { clearCatalogCaches, listingsFor, onCatalogsChanged } from "../lib/catalogs";
import { copyText } from "../lib/clipboard";
import { sqlIdentifier } from "../lib/completionContext";
import { type SchemaNode, parseSchemaName, schemaSql, schemaTree, spellSchemaPath } from "../lib/schemaPath";
import { type ServerState, type TlsMode, localServerId, serverLabel, useServers } from "../lib/servers";
import { sqlNameDragType } from "../lib/sqlNameDrag";
import { requestUpload } from "../lib/upload/request";
import { errorMessage, useSession } from "../session";
import { AddServerDialog } from "./AddServerDialog";
import { FloatingMenu, type MenuItem } from "./FloatingMenu";

type Load<T> = { state: "loading" } | { state: "error"; message: string } | { state: "done"; items: T };

/** Node keys: the server, then the path, JSON-encoded so no name can collide with a separator. */
const keyOf = (server: string, ...path: string[]) => JSON.stringify([server, ...path]);
/** Catalogs the browser leaves out: `temp` belongs to one session, and the browser isn't a tab's session. */
const hidden = new Set(["temp"]);
const previewRows = 100;
const searchDelayMs = 300;

function columnIcon(type: string) {
  const t = type.toUpperCase();
  if (/INT|DECIMAL|NUMERIC|FLOAT|DOUBLE|REAL|HUGEINT/.test(t)) return Hash;
  if (/VARCHAR|TEXT|STRING|CHAR|UUID|ENUM/.test(t)) return Type;
  if (/BOOL/.test(t)) return ToggleLeft;
  if (/DATE|TIME|INTERVAL/.test(t)) return Calendar;
  if (/BLOB|BIT|BYTEA/.test(t)) return Binary;
  if (/STRUCT|MAP|UNION|JSON|VARIANT/.test(t)) return Braces;
  if (/LIST|ARRAY|\[\]/.test(t)) return Columns3;
  return CircleDot;
}

/** The lock beside a server: how its connection is protected. */
function TlsLock({ mode, title }: { mode: TlsMode; title: string }) {
  if (mode === "plaintext") {
    return (
      <span className="tls-lock tls-lock-plaintext" title={title} data-tls={mode}>
        <LockOpen size={12} aria-label="Not encrypted" />
      </span>
    );
  }
  return (
    <span className={`tls-lock tls-lock-${mode}`} title={title} data-tls={mode}>
      <Lock
        size={12}
        aria-label={mode === "verified" ? "TLS, certificate verified" : "TLS, certificate trusted by its fingerprint"}
      />
    </span>
  );
}

function lockTitle(mode: TlsMode): string {
  switch (mode) {
    case "verified":
      return "TLS: the certificate was verified against trusted roots";
    case "fingerprint":
      return "TLS: the certificate is trusted by the fingerprint you accepted";
    case "plaintext":
      return "Plain HTTP: this connection is not encrypted";
  }
}

/** A server as the tree shows it: this one (when there is one), then the saved ones. */
interface TreeServer {
  id: string;
  label: string;
  tls: TlsMode;
  version: string;
  state?: ServerState;
  connected: boolean;
}

export function DataExplorer() {
  const session = useSession();
  const active = useActiveTab();
  const snapshot = useServers(session.servers);
  // This server starts open (as the tree did before servers had a node).
  const [open, setOpen] = useState<Set<string>>(() => new Set(session.localServer ? [keyOf(localServerId)] : []));
  const [loads, setLoads] = useState<Map<string, Load<unknown>>>(new Map());
  const [keywords, setKeywords] = useState<Set<string>>(new Set());
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuItem[] } | null>(null);
  const closeMenu = useCallback(() => setMenu(null), []);
  const [definition, setDefinition] = useState<{ name: string; sql?: string; error?: string } | null>(null);
  const [found, setFound] = useState<string | undefined>();
  const [term, setTerm] = useState("");
  const [searchResult, setSearchResult] = useState<
    { term: string; load: Load<{ server: TreeServer; hits: SearchHit[]; more: boolean; error?: string }[]> } | undefined
  >();
  const [notice, setNotice] = useState<string | undefined>();
  const [adding, setAdding] = useState<{ server?: ServerState } | null>(null);
  const tree = useRef<HTMLUListElement>(null);
  const [autoOpen] = useState(() => new Once());

  const servers = useMemo((): TreeServer[] => {
    const out: TreeServer[] = [];
    if (session.localServer) {
      out.push({
        id: localServerId,
        label: "This server",
        tls: window.location.protocol === "https:" ? "verified" : "plaintext",
        version: session.identity.version,
        connected: true,
      });
    }
    for (const state of snapshot.servers) {
      out.push({
        id: state.saved.id,
        label: serverLabel(state),
        tls: state.saved.tls,
        version: state.signIn?.version ?? "",
        state,
        connected: state.status === "connected" && !!state.signIn,
      });
    }
    return out;
  }, [session.localServer, session.identity.version, snapshot]);
  const serverOf = useCallback((id: string) => servers.find((s) => s.id === id), [servers]);

  // This server lists what the active tab's role may see; another server, all its user may.
  const cacheFor = useCallback(
    (server: string): CatalogCache => listingsFor(session, server === localServerId ? active.role : "", server),
    [session, active.role],
  );

  // Names that need quotes (mixed case, spaces, keywords) are written quoted.
  useEffect(() => {
    let live = true;
    const source = servers.find((s) => s.connected);
    if (!source) return;
    cacheFor(source.id)
      .vocabulary()
      .then(
        (v) => live && setKeywords(new Set(v.keywords.map((k) => k.toUpperCase()))),
        () => {},
      );
    return () => {
      live = false;
    };
  }, [cacheFor, servers]);
  const ident = useCallback((name: string) => sqlIdentifier(name, keywords), [keywords]);
  /** catalog.schema[.object], the schema in the server's spelling (nested ones are paths). */
  const qualified = useCallback(
    (catalog: string, schema?: string, ...rest: string[]) =>
      [
        ident(catalog),
        ...(schema === undefined ? [] : [schemaSql(parseSchemaName(schema), ident)]),
        ...rest.map(ident),
      ].join("."),
    [ident],
  );

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

  // The first time it can, the browser opens the active tab's server and catalog.
  const openActive = useCallback(() => {
    const tab = currentActiveTab();
    const catalog = tab.catalog;
    if (!catalog) return;
    cacheFor(tab.server)
      .catalogs()
      .then(
        (names) => {
          if (!names.includes(catalog) || !autoOpen.take()) return;
          setOpen((o) => new Set(o).add(keyOf(tab.server)).add(keyOf(tab.server, catalog)));
          load(keyOf(tab.server, catalog), () => cacheFor(tab.server).schemas(catalog));
        },
        () => {},
      );
  }, [cacheFor, load, autoOpen]);
  useEffect(() => subscribeActiveTab(openActive), [openActive]);

  const loadFor = useCallback(
    (server: string, path: string[]) => {
      const key = keyOf(server, ...path);
      const cache = cacheFor(server);
      if (path.length === 0) load(key, () => cache.catalogs(), openActive);
      else if (path.length === 1) load(key, () => cache.schemas(path[0]!));
      else if (path.length === 2) load(key, () => cache.tables(path[0]!, path[1]!));
      else if (path.length === 3) load(key, () => cache.columns(path[0]!, path[1]!, path[2]!));
    },
    [cacheFor, load, openActive],
  );

  // Everything open again: at the start, for a new role, after a run that may have created,
  // dropped or attached something, and when a server connects.
  const relist = useCallback(
    (paths: Set<string>) => {
      setLoads(new Map());
      for (const s of servers) if (s.connected && paths.has(keyOf(s.id))) loadFor(s.id, []);
      for (const key of paths) {
        const [server, ...path] = JSON.parse(key) as string[];
        if (server === undefined || path[0] === "group" || path.length === 0 || path.length > 3) continue;
        if (serverOf(server)?.connected) loadFor(server, path);
      }
    },
    [loadFor, servers, serverOf],
  );
  const openRef = useRef(open);
  useEffect(() => {
    openRef.current = open;
  }, [open]);
  useEffect(() => relist(openRef.current), [relist]);
  useEffect(() => onCatalogsChanged(() => relist(openRef.current)), [relist]);
  const toggle = (server: string, path: string[], group?: string) => {
    const key = group ? keyOf(server, "group", ...path, group) : keyOf(server, ...path);
    const opening = !open.has(key);
    setOpen((o) => {
      const next = new Set(o);
      if (next.has(key)) next.delete(key);
      else {
        next.add(key);
        // A schema opens with its Tables and Views groups open.
        if (!group && path.length === 2) {
          next.add(keyOf(server, "group", path[0]!, path[1]!, "tables"));
          next.add(keyOf(server, "group", path[0]!, path[1]!, "views"));
        }
      }
      return next;
    });
    if (opening && !group && !loads.has(key) && serverOf(server)?.connected) loadFor(server, path);
  };

  // ── Search ────────────────────────────────────────────────────────────────────────────────
  useEffect(() => {
    const t = term.trim();
    if (t.length < searchMinChars) return;
    let live = true;
    const timer = window.setTimeout(() => {
      const connected = servers.filter((s) => s.connected);
      void Promise.all(
        connected.map((server) =>
          cacheFor(server.id)
            .search(t)
            .then(
              (r) => ({ server, hits: r.hits, more: r.more }),
              (err: unknown) => {
                session.checkSignIn(err);
                return { server, hits: [], more: false, error: errorMessage(err) };
              },
            ),
        ),
      ).then((results) => live && setSearchResult({ term: t, load: { state: "done", items: results } }));
    }, searchDelayMs);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [term, cacheFor, session, servers]);

  const reveal = (server: string, hit: SearchHit) => {
    const schemaPath = parseSchemaName(hit.schema);
    const keys = [
      keyOf(server),
      keyOf(server, hit.catalog),
      ...schemaPath.map((_, i) => keyOf(server, hit.catalog, schemaSpelling(hit.schema, i + 1))),
      keyOf(server, "group", hit.catalog, hit.schema, hit.isView ? "views" : "tables"),
    ];
    setOpen((o) => new Set([...o, ...keys]));
    for (const path of [[], [hit.catalog], [hit.catalog, hit.schema]]) {
      if (!loads.has(keyOf(server, ...path))) loadFor(server, path);
    }
    const target = keyOf(server, hit.catalog, hit.schema, hit.name);
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
  const pointTabAt = (server: string, catalog: string, schema?: string) => {
    const why = session.pointTabAt(catalog, schema, server);
    if (why) setNotice(why);
  };
  const copy = (text: string) =>
    void copyText(text).then((ok) => setNotice(ok ? `Copied ${text}` : "The browser refused the copy."));
  const preview = (server: string, catalog: string, schema: string, name: string) =>
    session.requestQuery({
      sql: `SELECT * FROM ${qualified(catalog, schema, name)} LIMIT ${previewRows};`,
      run: true,
      newTab: true,
      title: name,
      catalog,
      schema,
      server,
    });
  const columnsOf = async (server: string, catalog: string, schema: string, name: string) =>
    (await cacheFor(server).columns(catalog, schema, name)).map((c) => ident(c.name));
  const showDefinition = (server: string, catalog: string, schema: string, name: string) => {
    setDefinition({ name: qualified(catalog, schema, name) });
    cacheFor(server)
      .viewDefinition(catalog, schema, name)
      .then(
        (sql) =>
          setDefinition({
            name: qualified(catalog, schema, name),
            ...(sql ? { sql } : { error: "The server has no definition for this view." }),
          }),
        (err: unknown) => setDefinition({ name: qualified(catalog, schema, name), error: errorMessage(err) }),
      );
  };

  const objectMenu = (server: string, catalog: string, schema: string, t: TableEntry): MenuItem[] => {
    const name = qualified(catalog, schema, t.name);
    return [
      { label: "Insert name in editor", run: () => insert(name) },
      { label: "Copy name", run: () => copy(name) },
      { label: "Preview data", run: () => preview(server, catalog, schema, t.name) },
      {
        label: "Insert column names",
        run: () =>
          void columnsOf(server, catalog, schema, t.name).then(
            (cols) => insert(cols.join(", ")),
            () => {},
          ),
      },
      { label: "Insert SELECT statement", run: () => insert(`SELECT * FROM ${name} LIMIT ${previewRows};`) },
      ...(t.isView ? [{ label: "Show definition", run: () => showDefinition(server, catalog, schema, t.name) }] : []),
      { label: "Use this catalog and schema", run: () => pointTabAt(server, catalog, schema), disabled: useBlocked },
    ];
  };
  const upload = (server: string, catalog?: string, schema?: string) =>
    requestUpload({
      catalog: catalog ?? (server === active.server ? active.catalog : ""),
      schema: schema ?? (catalog === undefined && server === active.server ? active.schema : ""),
      role: server === localServerId ? active.role : "",
      server,
    });
  const schemaMenu = (server: string, catalog: string, schema: string): MenuItem[] => [
    { label: "Insert name in editor", run: () => insert(qualified(catalog, schema)) },
    { label: "Copy name", run: () => copy(qualified(catalog, schema)) },
    { label: "Use this catalog and schema", run: () => pointTabAt(server, catalog, schema), disabled: useBlocked },
    { label: "Upload data…", run: () => upload(server, catalog, schema) },
  ];
  const catalogMenu = (server: string, catalog: string): MenuItem[] => [
    { label: "Insert name in editor", run: () => insert(ident(catalog)) },
    { label: "Copy name", run: () => copy(ident(catalog)) },
    {
      label: "Use this catalog",
      run: () =>
        void cacheFor(server)
          .schemas(catalog)
          .then(
            (names) => pointTabAt(server, catalog, names.includes("main") ? "main" : (names[0] ?? "")),
            (err: unknown) => setNotice(errorMessage(err)),
          ),
      disabled: useBlocked,
    },
    { label: "Upload data…", run: () => upload(server, catalog) },
  ];
  const columnMenu = (column: ColumnEntry): MenuItem[] => [
    { label: "Insert name in editor", run: () => insert(ident(column.name)) },
    { label: "Copy name", run: () => copy(ident(column.name)) },
  ];
  const serverMenu = (server: TreeServer): MenuItem[] => {
    const state = server.state;
    const items: MenuItem[] = [];
    if (server.connected) {
      items.push({
        label: "New tab on this server",
        run: () => session.requestQuery({ sql: "", run: false, newTab: true, server: server.id }),
      });
      items.push({ label: "Upload data…", run: () => upload(server.id) });
    }
    if (state) {
      if (server.connected) {
        items.push({
          label: "Disconnect",
          run: () => {
            session.servers.disconnect(state.saved.id);
            setNotice(`Disconnected from ${serverLabel(state)}.`);
          },
        });
      } else {
        items.push({ label: "Sign in…", run: () => setAdding({ server: state }) });
      }
      items.push({ label: "Copy URL", run: () => copy(state.saved.url) });
      items.push({
        label: "Rename…",
        run: () => {
          const name = window.prompt("Name for this server", state.saved.name);
          if (name?.trim()) session.servers.rename(state.saved.id, name.trim());
        },
      });
      items.push({
        label: "Remove from the list",
        run: () => {
          if (window.confirm(`Remove ${serverLabel(state)} from the list? Its tabs stay open but can't run.`)) {
            session.servers.remove(state.saved.id);
          }
        },
      });
    } else {
      items.push({ label: "Copy URL", run: () => copy(window.location.origin) });
    }
    return items;
  };

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
        <li className="tree-note" style={depthStyle(depth)} aria-busy="true">
          Loading…
        </li>
      );
    }
    if (l.state === "error") {
      return (
        <li className="tree-note tree-error" style={depthStyle(depth)} role="alert">
          <span>{l.message}</span>{" "}
          <button type="button" className="btn btn-ghost btn-sm" onClick={retry}>
            Retry
          </button>
        </li>
      );
    }
    if (Array.isArray(l.items) && l.items.length === 0) {
      return (
        <li className="tree-note" style={depthStyle(depth)}>
          {empty}
        </li>
      );
    }
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
    className?: string;
  }) => {
    const {
      key,
      depth,
      label,
      icon,
      expandable,
      expanded,
      onToggle,
      drag,
      detail,
      menu: m,
      title,
      className,
    } = options;
    return (
      <div
        className={`tree-row-wrap${found === key ? " tree-found" : ""}`}
        data-key={key}
        onContextMenu={m ? (e) => openMenu(e, m) : undefined}
      >
        <button
          type="button"
          className={`tree-row${className ? ` ${className}` : ""}`}
          style={depthStyle(depth)}
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

  const objects = (
    server: string,
    catalog: string,
    schema: string,
    depth: number,
    list: TableEntry[],
    kind: "tables" | "views",
  ) => {
    const groupKey = keyOf(server, "group", catalog, schema, kind);
    const groupOpen = open.has(groupKey);
    const label = kind === "tables" ? "Tables" : "Views";
    return (
      <li key={groupKey} role="treeitem" aria-expanded={groupOpen} aria-label={label}>
        {row({
          key: groupKey,
          depth,
          label: `${label} (${list.length.toLocaleString()})`,
          icon: <Layers size={14} className="tree-icon" />,
          expandable: true,
          expanded: groupOpen,
          onToggle: () => toggle(server, [catalog, schema], kind),
        })}
        {groupOpen && (
          <ul role="group">
            {list.map((t) => {
              const tk = keyOf(server, catalog, schema, t.name);
              const tOpen = open.has(tk);
              const cols = items(tk) as ColumnEntry[] | undefined;
              const name = qualified(catalog, schema, t.name);
              return (
                <li key={tk} role="treeitem" aria-expanded={tOpen} aria-label={t.name} data-table-type={t.type}>
                  {row({
                    key: tk,
                    depth: depth + 1,
                    label: t.name,
                    icon: t.isView ? (
                      <Eye size={14} className="tree-icon" />
                    ) : (
                      <Table2 size={14} className="tree-icon" />
                    ),
                    expandable: true,
                    expanded: tOpen,
                    onToggle: () => toggle(server, [catalog, schema, t.name]),
                    drag: name,
                    menu: objectMenu(server, catalog, schema, t),
                  })}
                  {tOpen && (
                    <ul role="group">
                      {cols?.map((c) => {
                        const Icon = columnIcon(c.type);
                        return (
                          <li key={c.name} role="treeitem" aria-label={c.name}>
                            {row({
                              key: keyOf(server, catalog, schema, t.name, c.name),
                              depth: depth + 2,
                              label: c.name,
                              icon: <Icon size={13} className="tree-icon" />,
                              drag: ident(c.name),
                              // The whole name and type, however narrow the panel has cut them.
                              title: `${c.name}  ${c.type}${c.nullable ? "" : " NOT NULL"}\nDouble-click or drag to insert ${ident(c.name)}`,
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
                      {status(loads.get(tk), depth + 2, () => loadFor(server, [catalog, schema, t.name]))}
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

  // A schema, with its nested schemas (DuckDB 2.0) under it, then its Tables and Views.
  const schemaItem = (server: string, catalog: string, node: SchemaNode, depth: number): ReactNode => {
    const schema = node.spelled;
    const sk = keyOf(server, catalog, schema);
    const sOpen = open.has(sk);
    const list = node.listed ? (items(sk) as TableEntry[] | undefined) : [];
    return (
      <li
        key={sk}
        role="treeitem"
        aria-expanded={sOpen}
        aria-label={node.name}
        data-schema-path={JSON.stringify(node.path)}
      >
        {row({
          key: sk,
          depth,
          label: node.name,
          icon: <FolderTree size={14} className="tree-icon" />,
          expandable: true,
          expanded: sOpen,
          onToggle: () => toggle(server, [catalog, schema]),
          drag: qualified(catalog, schema),
          menu: node.listed ? schemaMenu(server, catalog, schema) : undefined,
          title: node.listed
            ? undefined
            : `${qualified(catalog, schema)}: a schema you may not list, with schemas under it you may`,
        })}
        {sOpen && (
          <ul role="group">
            {node.children.map((child) => schemaItem(server, catalog, child, depth + 1))}
            {list &&
              objects(
                server,
                catalog,
                schema,
                depth + 1,
                list.filter((t) => !t.isView),
                "tables",
              )}
            {list &&
              objects(
                server,
                catalog,
                schema,
                depth + 1,
                list.filter((t) => t.isView),
                "views",
              )}
            {node.listed &&
              status(loads.get(sk), depth + 1, () => loadFor(server, [catalog, schema]), "No tables or views")}
          </ul>
        )}
      </li>
    );
  };

  const catalogItems = (server: TreeServer) => {
    const catalogs = ((items(keyOf(server.id)) as string[] | undefined) ?? []).filter((c) => !hidden.has(c));
    return (
      <>
        {catalogs.map((catalog) => {
          const ck = keyOf(server.id, catalog);
          const cOpen = open.has(ck);
          const schemas = items(ck) as string[] | undefined;
          return (
            <li key={ck} role="treeitem" aria-expanded={cOpen} aria-label={catalog}>
              {row({
                key: ck,
                depth: 1,
                label: catalog,
                icon: <Database size={14} className="tree-icon" />,
                expandable: true,
                expanded: cOpen,
                onToggle: () => toggle(server.id, [catalog]),
                drag: ident(catalog),
                menu: catalogMenu(server.id, catalog),
              })}
              {cOpen && (
                <ul role="group">
                  {schemas && schemaTree(schemas).map((node) => schemaItem(server.id, catalog, node, 2))}
                  {status(loads.get(ck), 2, () => loadFor(server.id, [catalog]), "No schemas")}
                </ul>
              )}
            </li>
          );
        })}
        {status(loads.get(keyOf(server.id)), 1, () => loadFor(server.id, []), "No catalogs")}
      </>
    );
  };

  const serverItem = (server: TreeServer) => {
    const key = keyOf(server.id);
    const isOpen = open.has(key);
    const state = server.state;
    const detail = (
      <>
        {server.version && (
          <span className="tree-tag" title="Server version">
            {server.version.split(" ")[0]}
          </span>
        )}
        {state && !server.connected && (
          <span className="tree-type tree-server-status" data-status={state.status} title={state.error}>
            {state.status === "error" ? "sign-in needed" : "not signed in"}
          </span>
        )}
      </>
    );
    return (
      <li
        key={key}
        role="treeitem"
        aria-expanded={isOpen}
        aria-label={server.label}
        data-server={server.id || "local"}
        data-connected={server.connected}
      >
        {row({
          key,
          depth: 0,
          label: server.label,
          className: "tree-server",
          icon: (
            <>
              <Server size={14} className="tree-icon" />
              <TlsLock mode={server.tls} title={lockTitle(server.tls)} />
            </>
          ),
          expandable: true,
          expanded: isOpen,
          onToggle: () => {
            if (state && !server.connected) {
              setAdding({ server: state });
              return;
            }
            toggle(server.id, []);
          },
          detail,
          menu: serverMenu(server),
          title: state
            ? `${state.saved.url}${state.signIn ? ` as ${state.signIn.username}` : ""}`
            : window.location.origin,
        })}
        {isOpen && server.connected && <ul role="group">{catalogItems(server)}</ul>}
      </li>
    );
  };

  const searching = term.trim().length >= searchMinChars;
  const results = searchResult && searchResult.term === term.trim() ? searchResult.load : undefined;
  const groups = results?.state === "done" ? results.items : undefined;
  const anyHit = groups?.some((g) => g.hits.length > 0) ?? false;
  const canAdd = session.contract.remote_servers;

  return (
    <div className="schema-browser" data-testid="data-explorer">
      <div className="sidebar-head">
        <FolderTree size={15} />
        <span>Data</span>
        {canAdd && (
          <button
            type="button"
            className="btn btn-ghost btn-icon btn-sm"
            onClick={() => setAdding({})}
            title="Add a server: connect to another Hardcharger server"
            aria-label="Add a server"
            data-testid="explorer-add-server"
          >
            <Plus size={14} />
          </button>
        )}
        <button
          type="button"
          className="btn btn-ghost btn-icon btn-sm"
          onClick={() => upload(active.server)}
          title="Upload data: a CSV, Excel, Parquet, JSON or Arrow file into a table"
          aria-label="Upload data"
          data-testid="explorer-upload"
          disabled={!serverOf(active.server)?.connected}
        >
          <FileUp size={14} />
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-icon btn-sm"
          // Every server's and role's listings, so the editor's catalog and schema pickers list again too.
          onClick={() => clearCatalogCaches(session)}
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
          {groups && !anyHit && !groups.some((g) => g.error) && (
            <li className="tree-note">No tables or views match.</li>
          )}
          {groups?.map((g) => (
            <li key={g.server.id} className="search-group">
              {(servers.length > 1 || g.error) && <div className="search-group-head">{g.server.label}</div>}
              {g.error && (
                <div className="tree-note tree-error" role="alert">
                  {g.error}
                </div>
              )}
              <ul>
                {g.hits.map((h) => (
                  <li key={keyOf(g.server.id, h.catalog, h.schema, h.name)}>
                    <button
                      type="button"
                      className="search-hit"
                      onClick={() => reveal(g.server.id, h)}
                      title="Show it in the tree"
                    >
                      {h.isView ? <Eye size={13} className="tree-icon" /> : <Table2 size={13} className="tree-icon" />}
                      <span className="tree-name">{h.name}</span>
                      <span className="tree-type">
                        {h.catalog}.{h.schema}
                      </span>
                    </button>
                  </li>
                ))}
                {g.more && <li className="tree-note">More than 100 match: type more of the name.</li>}
              </ul>
            </li>
          ))}
        </ul>
      )}
      <ul className="side-tree" role="tree" aria-label="Servers" ref={tree}>
        {servers.map(serverItem)}
        {servers.length === 0 && (
          <li className="tree-note tree-empty" data-testid="explorer-no-servers">
            No servers yet.{" "}
            {canAdd && (
              <button type="button" className="btn btn-primary btn-sm" onClick={() => setAdding({})}>
                <Plus size={13} /> Add a server
              </button>
            )}
          </li>
        )}
      </ul>

      {menu && <FloatingMenu x={menu.x} y={menu.y} items={menu.items} onClose={closeMenu} />}
      {adding && (
        <AddServerDialog
          {...(adding.server ? { server: adding.server } : {})}
          onConnected={(id) => {
            setAdding(null);
            setOpen((o) => new Set(o).add(keyOf(id)));
            loadFor(id, []);
            setNotice("Connected.");
          }}
          onClose={() => setAdding(null)}
        />
      )}
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

/** The spelling of the first `parts` parts of a nested schema's spelling (for its ancestors' keys). */
function schemaSpelling(spelled: string, parts: number): string {
  const path = parseSchemaName(spelled);
  if (parts >= path.length) return spelled;
  return spellSchemaPath(path.slice(0, parts));
}

/**
 * Indentation by depth. With servers at the root, a column sits one level deeper than it did:
 * the steps are a little smaller so a column row keeps the room it had (78 px in) in a narrow
 * panel.
 */
function depthStyle(depth: number): CSSProperties {
  const padding = [8, 22, 36, 50, 64, 78];
  return { paddingLeft: padding[depth] ?? 78 + (depth - 5) * 14 };
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
