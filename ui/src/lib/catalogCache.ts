// What the schema browser and autocomplete list: catalogs, schemas, tables and views, columns,
// and the SQL vocabulary (keywords, functions), read once through Warp SQL metadata (and
// DuckDB's own catalog functions for the vocabulary) and kept for a few minutes. The server
// lists only what the user's access control lets them see, through every one of these.
//
// One cache per connection (a role picked with SET ROLE lists through a connection of its
// own), shared by the browser and autocomplete, so whatever one has listed the other doesn't
// fetch again. A run that may change the catalog (CREATE, DROP, ATTACH, ...) clears it.

import type { Connection } from "@gizmodata/warp-sql";

import { exactLike } from "./sql";

const ttlMs = 5 * 60_000;

export interface TableEntry {
  name: string;
  type: string;
  isView: boolean;
}

export interface ColumnEntry {
  name: string;
  type: string;
  nullable: boolean;
}

export interface FunctionEntry {
  name: string;
  /** scalar, aggregate, table, macro, table_macro, pragma */
  type: string;
}

export interface Vocabulary {
  keywords: string[];
  functions: FunctionEntry[];
}

export interface SearchHit {
  catalog: string;
  schema: string;
  name: string;
  isView: boolean;
}

export const searchMinChars = 2;
export const searchLimit = 100;

const literal = (value: string) => `'${value.replaceAll("'", "''")}'`;

/** Tables and views whose name contains `term` (any case), in every catalog the user may see. */
export function searchSql(term: string): string {
  const pattern = literal(`%${term.replace(/[\\%_]/g, (c) => `\\${c}`)}%`);
  return (
    "SELECT database_name, schema_name, name, is_view FROM (" +
    "SELECT database_name, schema_name, table_name AS name, false AS is_view FROM duckdb_tables() " +
    "WHERE NOT internal AND NOT temporary " +
    "UNION ALL SELECT database_name, schema_name, view_name, true FROM duckdb_views() " +
    "WHERE NOT internal AND NOT temporary" +
    `) WHERE name ILIKE ${pattern} ESCAPE '\\' ORDER BY lower(name), database_name, schema_name LIMIT ${searchLimit + 1}`
  );
}

type Rows = Iterable<unknown>;
const records = (t: Rows) =>
  [...t].map((r) => (r as { toJSON?: () => unknown }).toJSON?.() ?? r) as Record<string, unknown>[];
const str = (v: unknown) =>
  typeof v === "string" ? v : v === null || v === undefined ? "" : (v as number | bigint | boolean).toString();

export class CatalogCache {
  readonly #conn: () => Promise<Connection>;
  readonly #entries = new Map<string, { at: number; value: Promise<unknown> }>();
  #generation = 0;

  constructor(conn: () => Promise<Connection>) {
    this.#conn = conn;
  }

  /** Bumped by clear(): what a listing was made from. */
  get generation(): number {
    return this.#generation;
  }

  /** Forgets every listing (the vocabulary stays: a run doesn't change it). */
  clear(): void {
    for (const key of [...this.#entries.keys()]) if (key !== "vocabulary") this.#entries.delete(key);
    this.#generation++;
  }

  #cached<T>(key: string, load: () => Promise<T>): Promise<T> {
    const hit = this.#entries.get(key);
    if (hit && Date.now() - hit.at < ttlMs) return hit.value as Promise<T>;
    const value = load();
    value.catch(() => {
      if (this.#entries.get(key)?.value === value) this.#entries.delete(key);
    });
    this.#entries.set(key, { at: Date.now(), value });
    return value;
  }

  catalogs(): Promise<string[]> {
    return this.#cached("catalogs", async () =>
      records(await (await this.#conn()).metadata.catalogs()).map((r) => str(r["catalog_name"])),
    );
  }

  schemas(catalog: string): Promise<string[]> {
    return this.#cached(JSON.stringify(["schemas", catalog]), async () =>
      records(await (await this.#conn()).metadata.dbSchemas({ catalog }))
        .filter((r) => r["catalog_name"] === catalog)
        .map((r) => str(r["db_schema_name"])),
    );
  }

  tables(catalog: string, schema: string): Promise<TableEntry[]> {
    return this.#cached(JSON.stringify(["tables", catalog, schema]), async () =>
      records(await (await this.#conn()).metadata.tables({ catalog, dbSchemaFilterPattern: exactLike(schema) }))
        .filter((r) => r["db_schema_name"] === schema)
        .map((r) => {
          const type = str(r["table_type"]);
          return { name: str(r["table_name"]), type, isView: type.toUpperCase().includes("VIEW") };
        }),
    );
  }

  columns(catalog: string, schema: string, table: string): Promise<ColumnEntry[]> {
    return this.#cached(JSON.stringify(["columns", catalog, schema, table]), async () => {
      const conn = await this.#conn();
      const found = await conn.metadata.columns({
        catalog,
        dbSchemaFilterPattern: exactLike(schema),
        tableNameFilterPattern: exactLike(table),
      });
      return found
        .filter((c) => c.dbSchemaName === schema && c.tableName === table)
        .map((c) => ({ name: c.columnName, type: c.typeName ?? String(c.field.type), nullable: c.nullable }));
    });
  }

  /** The engine's keywords and functions (as the user may see them), for autocomplete. */
  vocabulary(): Promise<Vocabulary> {
    return this.#cached("vocabulary", async () => {
      const conn = await this.#conn();
      const [k, f] = await Promise.all([
        conn.query("SELECT DISTINCT keyword_name FROM duckdb_keywords() ORDER BY 1"),
        conn.query(
          "SELECT DISTINCT function_name, function_type FROM duckdb_functions() " +
            "WHERE NOT internal OR function_type IN ('scalar', 'aggregate', 'table', 'macro', 'table_macro') " +
            "ORDER BY 1",
        ),
      ]);
      return {
        keywords: records(k).map((r) => str(r["keyword_name"])),
        functions: records(f)
          .map((r) => ({ name: str(r["function_name"]), type: str(r["function_type"]) }))
          .filter((fn) => /^[a-z_][a-z0-9_]*$/i.test(fn.name)),
      };
    });
  }

  /** Tables and views whose name contains `term` (at most searchLimit, and whether there were more). */
  async search(term: string): Promise<{ hits: SearchHit[]; more: boolean }> {
    const conn = await this.#conn();
    const rows = records(await conn.query(searchSql(term)));
    const hits = rows.map((r) => ({
      catalog: str(r["database_name"]),
      schema: str(r["schema_name"]),
      name: str(r["name"]),
      isView: r["is_view"] === true,
    }));
    return { hits: hits.slice(0, searchLimit), more: hits.length > searchLimit };
  }

  /** A view's CREATE VIEW statement, as the engine keeps it. */
  async viewDefinition(catalog: string, schema: string, view: string): Promise<string | undefined> {
    const conn = await this.#conn();
    const rows = records(
      await conn.query(
        "SELECT sql FROM duckdb_views() WHERE database_name = $1 AND schema_name = $2 AND view_name = $3",
        [catalog, schema, view],
      ),
    );
    const sql = rows[0]?.["sql"];
    return typeof sql === "string" ? sql : undefined;
  }
}
