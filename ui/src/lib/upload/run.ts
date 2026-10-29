// An upload's run on the server: its own Chargewire session (acting with the tab's role), one
// transaction, and the file's batches sent one bulk-ingest request each (§8.1), in the file's
// order: the first creates (or replaces) the table, the rest append. Nothing is visible to
// anyone else until the commit, and a failure or a cancel rolls everything back, the table
// as it was. As GizmoData Cloud does it:
//
//   create   fails up front if the table exists;
//   append   fails up front if it doesn't; the file's columns are matched to the table's by
//            name (the batches are built in the table's column order, NULL for a column the
//            file lacks), and the server converts each value to its column's type;
//   replace  replaces the table (its columns and rows) with the file's, in the transaction.
//
// Why one transaction of ordered requests, not a partitioned ingest (§8.3): a browser can't
// stream a request body (Safari can't at all, Chromium only over HTTP/2), so either way each
// request holds a batch in memory; a partitioned ingest gives the rows no order (§8.3) and
// sends them over several connections, which the browser's parsing, not the network, would
// leave idle. The single-request ingest keeps the file's row order, as GizmoData Cloud does.

import { type Connection, ErrorCode, ChargewireError } from "@gizmodata/chargewire";

import type { DataType } from "apache-arrow";

import { arrowTypeLabel } from "../format";
import { setRoleSql } from "../roles";
import { exactLike, quoteIdentifier } from "../sql";
import type { ReadChunk } from "./reader";

export type UploadMode = "create" | "append" | "replace";

export interface UploadTarget {
  /** Empty: the session's. */
  catalog: string;
  schema: string;
  table: string;
  createSchema: boolean;
  mode: UploadMode;
}

export interface UploadProgress {
  phase: "uploading" | "committing";
  fraction: number;
  rows: number;
  bytes: number;
}

export interface UploadResult {
  catalog: string;
  schema: string;
  table: string;
  mode: UploadMode;
  format: string;
  rows: number;
  seconds: number;
  columns: { name: string; type: string }[];
}

/** The catalog and schema a session is on. */
export async function sessionWhere(conn: Connection): Promise<{ catalog: string; schema: string }> {
  const row = (await conn.query("SELECT current_database() AS c, current_schema() AS s")).get(0)?.toJSON() as
    { c?: string; s?: string } | undefined;
  return { catalog: row?.c ?? "", schema: row?.s ?? "main" };
}

/** Whether a table (or view) by that name is listed there, as the user may see it. */
export async function tableExists(conn: Connection, catalog: string, schema: string, table: string): Promise<boolean> {
  const found = await conn.metadata.tables({
    catalog,
    dbSchemaFilterPattern: exactLike(schema),
    tableNameFilterPattern: exactLike(table),
  });
  for (const row of found) {
    const r = (row as { toJSON: () => Record<string, unknown> }).toJSON();
    const name = r["table_name"];
    if (r["db_schema_name"] === schema && typeof name === "string" && name.toLowerCase() === table.toLowerCase()) {
      return true;
    }
  }
  return false;
}

/** A table's columns and their types, as the user may see them. */
export async function tableColumns(
  conn: Connection,
  catalog: string,
  schema: string,
  table: string,
): Promise<{ name: string; type: string }[]> {
  const found = await conn.metadata.columns({
    catalog,
    dbSchemaFilterPattern: exactLike(schema),
    tableNameFilterPattern: exactLike(table),
  });
  return found
    .filter((c) => c.dbSchemaName === schema && c.tableName.toLowerCase() === table.toLowerCase())
    .map((c) => {
      // The metadata's type name has no precision for a DECIMAL; the column's Arrow type does.
      const named = c.typeName?.trim();
      const type = !named || /^(DECIMAL|NUMERIC)$/i.test(named) ? arrowTypeLabel(c.field.type as DataType) : named;
      return { name: c.columnName, type };
    });
}

export class UploadRefused extends Error {}

/** Opens the upload's session, acting with `role` ("" for all of the user's roles). */
export async function uploadConnection(
  open: (options: { reconnect: false }) => Promise<Connection>,
  role: string,
): Promise<Connection> {
  const conn = await open({ reconnect: false });
  if (role) {
    try {
      await conn.execute(setRoleSql(role));
    } catch (err) {
      void conn.close().catch(() => {});
      throw err;
    }
  }
  return conn;
}

/** Runs an upload: checks the target, then sends every chunk in one transaction. */
export async function runUpload(options: {
  conn: Connection;
  target: UploadTarget;
  format: string;
  chunks: AsyncIterable<ReadChunk>;
  signal: AbortSignal;
  onProgress: (p: UploadProgress) => void;
}): Promise<UploadResult> {
  const { conn, target, signal, onProgress } = options;
  const started = performance.now();
  const here = await sessionWhere(conn);
  const catalog = target.catalog || here.catalog;
  const schema = target.schema || here.schema || "main";
  const table = target.table.trim();
  const shown = `${catalog}.${schema}.${table}`;
  const exists = await tableExists(conn, catalog, schema, table);
  if (target.mode === "create" && exists) {
    throw new UploadRefused(`Table ${shown} already exists — choose Append or Replace, or another name.`);
  }
  if (target.mode === "append" && !exists) {
    throw new UploadRefused(`Table ${shown} doesn't exist — choose Create.`);
  }
  signal.throwIfAborted();
  await conn.execute("BEGIN TRANSACTION");
  let committed = false;
  try {
    if (target.createSchema) {
      await conn.execute(`CREATE SCHEMA IF NOT EXISTS ${quoteIdentifier(catalog)}.${quoteIdentifier(schema)}`);
    }
    let rows = 0;
    let bytes = 0;
    let first = true;
    for await (const chunk of options.chunks) {
      signal.throwIfAborted();
      const mode = first ? target.mode : "append";
      try {
        await conn.ingest(table, chunk.table, { mode, catalog, schema, signal });
      } catch (err) {
        if (target.mode === "append" && ChargewireError.is(err) && err.code !== ErrorCode.statementCancelled) {
          throw new UploadRefused(
            `The file's columns don't fit ${shown} (append matches columns by name): ${err.detail ?? err.message}`,
          );
        }
        throw err;
      }
      first = false;
      rows += chunk.rows;
      bytes += chunk.bytes;
      onProgress({ phase: "uploading", fraction: chunk.fraction, rows, bytes });
    }
    signal.throwIfAborted();
    onProgress({ phase: "committing", fraction: 1, rows, bytes });
    await conn.execute("COMMIT");
    committed = true;
    let columns: { name: string; type: string }[] = [];
    try {
      columns = await tableColumns(conn, catalog, schema, table);
    } catch {
      // the upload is done; the list of columns is a nicety
    }
    return {
      catalog,
      schema,
      table,
      mode: target.mode,
      format: options.format,
      rows,
      seconds: (performance.now() - started) / 1000,
      columns,
    };
  } finally {
    if (!committed) await conn.execute("ROLLBACK").catch(() => {});
  }
}
