// Running a script: its statements one after another on the tab's session, stopping at the
// first error (or at a cancel), each result set kept for its own result tab. The browser
// holds every result in memory, so a script keeps at most `maxResultSets` result sets and
// `maxResultBytes` of Arrow data in all; the rest still run, and are listed without rows.

import { ErrorCode, WarpSqlError } from "@gizmodata/warp-sql";
import { type RecordBatch, type Schema, Table } from "apache-arrow";

export const maxResultSets = 50;
export const maxResultBytes = 64 * 1024 * 1024;

/** What running one statement gives back: the parts of the client's ResultStream used here. */
export interface StatementStream extends AsyncIterable<RecordBatch> {
  rowsAffected(): Promise<number | null | undefined>;
  schema(): Promise<Schema>;
  close(): Promise<void>;
}

export type Execute = (sql: string, signal: AbortSignal) => StatementStream;

export interface StatementRun {
  index: number;
  sql: string;
  status: "rows" | "update" | "error" | "cancelled" | "not-run";
  elapsedMs: number;
  /** Rows read (rows), or changed (update; null when the engine doesn't count). */
  rows?: number;
  rowsAffected?: number | null;
  /** More rows than the row limit: only the first `rowLimit` were read. */
  truncated?: boolean;
  /** Index into ScriptRun.resultSets, or undefined when it has no rows or was left out. */
  resultSet?: number;
  /** Left out of the results (over the script's caps): run it on its own to see it. */
  omitted?: boolean;
  error?: unknown;
}

export interface ResultSet {
  statement: number;
  table: Table;
  truncated: boolean;
  bytes: number;
}

export interface ScriptRun {
  statements: StatementRun[];
  resultSets: ResultSet[];
  /** The statement that failed or was cancelled, if one did. */
  stoppedAt?: number;
  elapsedMs: number;
}

export interface Progress {
  statement: number;
  rows: number;
}

/** The Arrow buffers' size of a batch (what the browser holds for it). */
export function batchBytes(batch: RecordBatch): number {
  const size = (batch.data as { byteLength?: unknown }).byteLength;
  return typeof size === "number" ? size : 0;
}

export async function runScript(options: {
  statements: readonly string[];
  execute: Execute;
  rowLimit: number;
  signal: AbortSignal;
  onProgress?: (p: Progress) => void;
  /** Called after each statement ends, before the next one starts (for a session check). */
  afterStatement?: (run: StatementRun) => Promise<void>;
  now?: () => number;
  /** The caps (default: maxResultSets, maxResultBytes). */
  limits?: { resultSets?: number; bytes?: number };
}): Promise<ScriptRun> {
  const capSets = options.limits?.resultSets ?? maxResultSets;
  const capBytes = options.limits?.bytes ?? maxResultBytes;
  const { statements, execute, rowLimit, signal } = options;
  const now = options.now ?? (() => performance.now());
  const started = now();
  const runs: StatementRun[] = statements.map((sql, index) => ({ index, sql, status: "not-run", elapsedMs: 0 }));
  const resultSets: ResultSet[] = [];
  let bytes = 0;
  let stoppedAt: number | undefined;

  for (let i = 0; i < statements.length; i++) {
    if (signal.aborted) {
      runs[i]!.status = "cancelled";
      stoppedAt = i;
      break;
    }
    const run = runs[i]!;
    const at = now();
    try {
      const stream = execute(statements[i]!, signal);
      const affected = await stream.rowsAffected();
      if (affected !== undefined) {
        run.status = "update";
        run.rowsAffected = affected;
      } else {
        const schema = await stream.schema();
        const keep = resultSets.length < capSets && bytes < capBytes;
        const batches: RecordBatch[] = [];
        let rows = 0;
        let held = 0;
        let lastReport = 0;
        let truncated = false;
        for await (const batch of stream) {
          let piece = batch;
          if (rows + batch.numRows > rowLimit) {
            piece = batch.slice(0, rowLimit - rows);
            truncated = true;
          }
          rows += piece.numRows;
          if (keep) {
            batches.push(piece);
            held += batchBytes(piece);
          }
          if (now() - lastReport > 200) {
            lastReport = now();
            options.onProgress?.({ statement: i, rows });
          }
          if (truncated) break; // closing the stream releases the statement on the server
        }
        run.status = "rows";
        run.rows = rows;
        run.truncated = truncated;
        if (keep && bytes + held <= capBytes) {
          run.resultSet = resultSets.length;
          resultSets.push({ statement: i, table: new Table(schema, batches), truncated, bytes: held });
          bytes += held;
        } else {
          run.omitted = true;
        }
      }
    } catch (err) {
      run.status =
        signal.aborted || (WarpSqlError.is(err) && err.code === ErrorCode.statementCancelled) ? "cancelled" : "error";
      run.error = err;
      stoppedAt = i;
    }
    run.elapsedMs = now() - at;
    await options.afterStatement?.(run);
    if (stoppedAt !== undefined) break;
  }
  return {
    statements: runs,
    resultSets,
    ...(stoppedAt === undefined ? {} : { stoppedAt }),
    elapsedMs: now() - started,
  };
}

const plural = (n: number, word: string) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;

/** "Statement 2 of 5 was cancelled; statement 1 ran, 3 to 5 did not." */
export function cancelNote(run: ScriptRun): string {
  const n = run.statements.length;
  const at = run.stoppedAt ?? 0;
  if (n === 1) return "The query was cancelled. Nothing from it was saved.";
  const ran = at === 0 ? "no statement before it ran" : at === 1 ? "statement 1 ran" : `statements 1 to ${at} ran`;
  const rest = at + 1 >= n ? "" : at + 2 === n ? `, statement ${n} did not run` : `, ${at + 2} to ${n} did not run`;
  return `Statement ${at + 1} of ${n} was cancelled; nothing from it was saved (${ran}${rest}).`;
}

/** The line a statement gets in Messages. */
export function messageFor(s: StatementRun): string {
  switch (s.status) {
    case "rows":
      return `${plural(s.rows ?? 0, "row")}${s.truncated ? " (row limit)" : ""}${s.omitted ? ", not kept" : ""}`;
    case "update":
      return s.rowsAffected === null || s.rowsAffected === undefined
        ? "ok"
        : `${plural(s.rowsAffected, "row")} affected`;
    case "error":
      return "failed";
    case "cancelled":
      return "cancelled";
    case "not-run":
      return "not run";
  }
}
