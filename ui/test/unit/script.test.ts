import { type RecordBatch, Schema, tableFromArrays } from "apache-arrow";
import { describe, expect, it } from "vitest";

import {
  type Execute,
  type StatementStream,
  cancelNote,
  maxResultSets,
  messageFor,
  runScript,
} from "../../src/lib/script";

function rows(n: number): StatementStream {
  const table = tableFromArrays({ v: Int32Array.from({ length: n }, (_, i) => i) });
  return {
    rowsAffected: async () => undefined,
    schema: async () => table.schema,
    close: async () => {},
    async *[Symbol.asyncIterator]() {
      for (const b of table.batches) yield b as RecordBatch;
    },
  };
}

function update(n: number | null): StatementStream {
  return {
    rowsAffected: async () => n,
    schema: async () => new Schema([]),
    close: async () => {},
    async *[Symbol.asyncIterator]() {
      yield* [];
    },
  };
}

function failing(message: string): StatementStream {
  const err = Object.assign(new Error(message), { code: "sql_error" });
  return {
    rowsAffected: () => Promise.reject(err),
    schema: () => Promise.reject(err),
    close: async () => {},
    async *[Symbol.asyncIterator]() {
      yield* [];
      throw err;
    },
  };
}

const fake =
  (answers: Record<string, () => StatementStream>, ran: string[] = []): Execute =>
  (sql) => {
    ran.push(sql);
    const answer = answers[sql];
    if (!answer) throw new Error(`unexpected ${sql}`);
    return answer();
  };

describe("runScript", () => {
  it("runs every statement in order, a result set each, and the counts", async () => {
    const ran: string[] = [];
    const run = await runScript({
      statements: ["CREATE TABLE t", "SELECT 3", "INSERT 2", "SELECT 5"],
      execute: fake(
        {
          "CREATE TABLE t": () => update(null),
          "SELECT 3": () => rows(3),
          "INSERT 2": () => update(2),
          "SELECT 5": () => rows(5),
        },
        ran,
      ),
      rowLimit: 1000,
      signal: new AbortController().signal,
    });
    expect(ran).toEqual(["CREATE TABLE t", "SELECT 3", "INSERT 2", "SELECT 5"]);
    expect(run.stoppedAt).toBeUndefined();
    expect(run.resultSets.map((r) => [r.statement, r.table.numRows])).toEqual([
      [1, 3],
      [3, 5],
    ]);
    expect(run.statements.map(messageFor)).toEqual(["ok", "3 rows", "2 rows affected", "5 rows"]);
  });

  it("stops at the first error: the rest don't run", async () => {
    const ran: string[] = [];
    const run = await runScript({
      statements: ["SELECT 1", "SELECT nope", "SELECT 2"],
      execute: fake({ "SELECT 1": () => rows(1), "SELECT nope": () => failing("no such column") }, ran),
      rowLimit: 1000,
      signal: new AbortController().signal,
    });
    expect(ran).toEqual(["SELECT 1", "SELECT nope"]);
    expect(run.stoppedAt).toBe(1);
    expect(run.statements.map((s) => s.status)).toEqual(["rows", "error", "not-run"]);
    expect((run.statements[1]!.error as Error).message).toBe("no such column");
  });

  it("reads no more than the row limit, and says it cut the result", async () => {
    const run = await runScript({
      statements: ["SELECT big", "SELECT exact"],
      execute: fake({ "SELECT big": () => rows(2500), "SELECT exact": () => rows(100) }),
      rowLimit: 100,
      signal: new AbortController().signal,
    });
    expect(run.resultSets.map((r) => [r.table.numRows, r.truncated])).toEqual([
      [100, true],
      [100, false],
    ]);
    expect(messageFor(run.statements[0]!)).toBe("100 rows (row limit)");
  });

  it("keeps at most 50 result sets; the later ones run and are listed without rows", async () => {
    const statements = Array.from({ length: maxResultSets + 2 }, () => "SELECT 1");
    const ran: string[] = [];
    const run = await runScript({
      statements,
      execute: fake({ "SELECT 1": () => rows(1) }, ran),
      rowLimit: 10,
      signal: new AbortController().signal,
    });
    expect(ran).toHaveLength(52);
    expect(run.resultSets).toHaveLength(50);
    expect(run.statements.slice(50).map((s) => [s.status, s.omitted])).toEqual([
      ["rows", true],
      ["rows", true],
    ]);
  });

  it("a cancel stops the statement running and every one after it", async () => {
    const abort = new AbortController();
    const run = await runScript({
      statements: ["SELECT 1", "SELECT slow", "SELECT 2"],
      execute: (sql, signal) => {
        if (sql === "SELECT 1") return rows(1);
        return {
          rowsAffected: () =>
            new Promise((_, reject) => {
              signal.addEventListener("abort", () => reject(new Error("cancelled")));
              abort.abort();
            }),
          schema: async () => new Schema([]),
          close: async () => {},
          async *[Symbol.asyncIterator]() {
            yield* [];
          },
        };
      },
      rowLimit: 10,
      signal: abort.signal,
    });
    expect(run.statements.map((s) => s.status)).toEqual(["rows", "cancelled", "not-run"]);
    expect(cancelNote(run)).toBe(
      "Statement 2 of 3 was cancelled; nothing from it was saved (statement 1 ran, statement 3 did not run).",
    );
  });
});

describe("cancelNote", () => {
  const ofStops = (n: number, at: number) =>
    cancelNote({
      statements: Array.from({ length: n }, (_, index) => ({ index, sql: "", status: "not-run", elapsedMs: 0 })),
      resultSets: [],
      stoppedAt: at,
      elapsedMs: 0,
    });
  it("says how far a script got", () => {
    expect(ofStops(1, 0)).toBe("The query was cancelled. Nothing from it was saved.");
    expect(ofStops(4, 0)).toBe(
      "Statement 1 of 4 was cancelled; nothing from it was saved (no statement before it ran, 2 to 4 did not run).",
    );
    expect(ofStops(4, 3)).toBe("Statement 4 of 4 was cancelled; nothing from it was saved (statements 1 to 3 ran).");
  });
});
