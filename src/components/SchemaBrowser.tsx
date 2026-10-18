import type { ColumnInfo } from "@gizmodata/warp-sql";
import { ChevronDown, ChevronRight, Columns3, Database, Eye, FolderTree, RefreshCw, Table2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { completionNames } from "../lib/names";
import { exactLike, quoteIdentifier } from "../lib/sql";
import { errorMessage, useSession } from "../session";

type Load<T> = { state: "loading" } | { state: "error"; message: string } | { state: "done"; items: T[] };

interface TableItem {
  name: string;
  type: string;
}

const key = (...parts: string[]) => JSON.stringify(parts);

export function SchemaBrowser() {
  const session = useSession();
  const { connection } = session;
  const [catalogs, setCatalogs] = useState<Load<string>>({ state: "loading" });
  const [schemas, setSchemas] = useState<Record<string, Load<string>>>({});
  const [tables, setTables] = useState<Record<string, Load<TableItem>>>({});
  const [columns, setColumns] = useState<Record<string, Load<ColumnInfo>>>({});
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [generation, setGeneration] = useState(0);
  const [filter, setFilter] = useState("");

  const fail = useCallback(
    (err: unknown): string => {
      session.checkSignIn(err);
      return errorMessage(err);
    },
    [session],
  );

  const loadSchemas = useCallback(
    (catalog: string) => {
      const k = key(catalog);
      setSchemas((s) => ({ ...s, [k]: { state: "loading" } }));
      connection.metadata.dbSchemas({ catalog }).then(
        (t) => {
          const items = [...t]
            .map((r) => r as { catalog_name: string | null; db_schema_name: string })
            .filter((r) => r.catalog_name === catalog)
            .map((r) => r.db_schema_name);
          setSchemas((s) => ({ ...s, [k]: { state: "done", items } }));
        },
        (err: unknown) => setSchemas((s) => ({ ...s, [k]: { state: "error", message: fail(err) } })),
      );
    },
    [connection, fail],
  );

  const loadTables = useCallback(
    (catalog: string, schema: string) => {
      const k = key(catalog, schema);
      setTables((s) => ({ ...s, [k]: { state: "loading" } }));
      connection.metadata.tables({ catalog, dbSchemaFilterPattern: exactLike(schema) }).then(
        (t) => {
          const items = [...t]
            .map((r) => r as { db_schema_name: string | null; table_name: string; table_type: string })
            .filter((r) => r.db_schema_name === schema)
            .map((r) => ({ name: r.table_name, type: r.table_type }));
          for (const item of items) completionNames.add(item.name);
          setTables((s) => ({ ...s, [k]: { state: "done", items } }));
        },
        (err: unknown) => setTables((s) => ({ ...s, [k]: { state: "error", message: fail(err) } })),
      );
    },
    [connection, fail],
  );

  const loadColumns = useCallback(
    (catalog: string, schema: string, table: string) => {
      const k = key(catalog, schema, table);
      setColumns((s) => ({ ...s, [k]: { state: "loading" } }));
      connection.metadata
        .columns({ catalog, dbSchemaFilterPattern: exactLike(schema), tableNameFilterPattern: exactLike(table) })
        .then(
          (items) => {
            const mine = items.filter((c) => c.dbSchemaName === schema && c.tableName === table);
            for (const c of mine) completionNames.add(c.columnName);
            setColumns((s) => ({ ...s, [k]: { state: "done", items: mine } }));
          },
          (err: unknown) => setColumns((s) => ({ ...s, [k]: { state: "error", message: fail(err) } })),
        );
    },
    [connection, fail],
  );

  useEffect(() => {
    let live = true;
    connection.metadata.catalogs().then(
      (t) => {
        if (!live) return;
        const items = [...t].map((r) => (r as { catalog_name: string }).catalog_name);
        setCatalogs({ state: "done", items });
        // The session's own catalog starts open.
        connection
          .getSession()
          .then((doc) => {
            const current = doc.options?.["catalog"];
            if (live && typeof current === "string" && items.includes(current)) {
              setOpen((o) => new Set(o).add(key(current)));
              loadSchemas(current);
            }
          })
          .catch(() => {});
      },
      (err: unknown) => live && setCatalogs({ state: "error", message: fail(err) }),
    );
    return () => {
      live = false;
    };
  }, [connection, generation, fail, loadSchemas]);

  // Opening a node loads what it shows, the first time.
  const toggle = (k: string) => {
    const opening = !open.has(k);
    setOpen((o) => {
      const next = new Set(o);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
    if (!opening) return;
    const parts = JSON.parse(k) as string[];
    if (parts.length === 1 && !schemas[k]) loadSchemas(parts[0]!);
    if (parts.length === 2 && !tables[k]) loadTables(parts[0]!, parts[1]!);
    if (parts.length === 3 && !columns[k]) loadColumns(parts[0]!, parts[1]!, parts[2]!);
  };

  const refresh = () => {
    setCatalogs({ state: "loading" });
    setOpen(new Set());
    setSchemas({});
    setTables({});
    setColumns({});
    setGeneration((g) => g + 1);
  };

  const qualified = (catalog: string, schema: string, table: string) =>
    [catalog, schema, table].map(quoteIdentifier).join(".");

  const needle = filter.trim().toLowerCase();
  const shown = (name: string) => needle === "" || name.toLowerCase().includes(needle);

  const status = (load: Load<unknown> | undefined, depth: number) => {
    if (!load || load.state === "loading")
      return (
        <li className={`tree-note depth-${depth}`} aria-busy="true">
          Loading…
        </li>
      );
    if (load.state === "error")
      return (
        <li className={`tree-note tree-error depth-${depth}`} role="alert">
          {load.message}
        </li>
      );
    if (load.items.length === 0) return <li className={`tree-note depth-${depth}`}>Empty</li>;
    return null;
  };

  return (
    <div className="schema-browser">
      <div className="sidebar-head">
        <FolderTree size={15} />
        <span>Schema</span>
        <button
          type="button"
          className="btn btn-ghost btn-icon btn-sm"
          onClick={refresh}
          title="Refresh"
          aria-label="Refresh the schema browser"
        >
          <RefreshCw size={14} />
        </button>
      </div>
      <input
        className="tree-filter"
        type="search"
        placeholder="Filter tables…"
        aria-label="Filter tables"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      <ul className="tree" role="tree" aria-label="Catalogs">
        {catalogs.state !== "done"
          ? status(catalogs, 0)
          : catalogs.items.map((catalog) => {
              const ck = key(catalog);
              const isOpen = open.has(ck);
              const s = schemas[ck];
              return (
                <li key={ck} role="treeitem" aria-expanded={isOpen} aria-label={catalog}>
                  <button type="button" className="tree-row depth-0" onClick={() => toggle(ck)}>
                    {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    <Database size={14} className="tree-icon" />
                    <span className="tree-name">{catalog}</span>
                  </button>
                  {isOpen && (
                    <ul role="group">
                      {s?.state === "done"
                        ? s.items.map((schema) => {
                            const sk = key(catalog, schema);
                            const sOpen = open.has(sk);
                            const t = tables[sk];
                            return (
                              <li key={sk} role="treeitem" aria-expanded={sOpen} aria-label={schema}>
                                <button type="button" className="tree-row depth-1" onClick={() => toggle(sk)}>
                                  {sOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                                  <FolderTree size={14} className="tree-icon" />
                                  <span className="tree-name">{schema}</span>
                                </button>
                                {sOpen && (
                                  <ul role="group">
                                    {t?.state === "done"
                                      ? t.items
                                          .filter((item) => shown(item.name))
                                          .map((item) => {
                                            const tk = key(catalog, schema, item.name);
                                            const tOpen = open.has(tk);
                                            const c = columns[tk];
                                            const name = qualified(catalog, schema, item.name);
                                            return (
                                              <li
                                                key={tk}
                                                role="treeitem"
                                                aria-expanded={tOpen}
                                                aria-label={item.name}
                                                data-table-type={item.type}
                                              >
                                                <div className="tree-row-wrap">
                                                  <button
                                                    type="button"
                                                    className="tree-row depth-2"
                                                    onClick={() => toggle(tk)}
                                                    onDoubleClick={() => session.insertText(name)}
                                                    title={`${item.type}: double-click to insert ${name}`}
                                                  >
                                                    {tOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                                                    <Table2 size={14} className="tree-icon" />
                                                    <span className="tree-name">{item.name}</span>
                                                    {item.type.includes("VIEW") && (
                                                      <span className="tree-tag">view</span>
                                                    )}
                                                  </button>
                                                  <button
                                                    type="button"
                                                    className="btn btn-ghost btn-icon btn-sm tree-action"
                                                    title={`Preview ${item.name}`}
                                                    aria-label={`Preview ${item.name}`}
                                                    onClick={() =>
                                                      session.requestQuery({
                                                        sql: `SELECT * FROM ${name} LIMIT 100;`,
                                                        run: true,
                                                        newTab: true,
                                                        title: item.name,
                                                      })
                                                    }
                                                  >
                                                    <Eye size={14} />
                                                  </button>
                                                </div>
                                                {tOpen && (
                                                  <ul role="group">
                                                    {c?.state === "done"
                                                      ? c.items.map((col) => (
                                                          <li
                                                            key={col.columnName}
                                                            role="treeitem"
                                                            aria-label={col.columnName}
                                                            className="tree-row depth-3 tree-column"
                                                            title="Double-click to insert the column name"
                                                            onDoubleClick={() =>
                                                              session.insertText(quoteIdentifier(col.columnName))
                                                            }
                                                          >
                                                            <Columns3 size={13} className="tree-icon" />
                                                            <span className="tree-name">{col.columnName}</span>
                                                            <span className="tree-type">
                                                              {col.typeName ?? String(col.field.type)}
                                                              {col.nullable ? "" : " NOT NULL"}
                                                            </span>
                                                          </li>
                                                        ))
                                                      : null}
                                                    {status(c, 3)}
                                                  </ul>
                                                )}
                                              </li>
                                            );
                                          })
                                      : null}
                                    {status(t, 2)}
                                  </ul>
                                )}
                              </li>
                            );
                          })
                        : null}
                      {status(s, 1)}
                    </ul>
                  )}
                </li>
              );
            })}
        {catalogs.state === "done" && status(catalogs, 0)}
      </ul>
    </div>
  );
}
