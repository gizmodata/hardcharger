import { describe, expect, it } from "vitest";
import {
  activeQuery,
  defaultMonitorSort,
  monitorElapsed,
  monitorQueueWait,
  nextMonitorSort,
  sortMonitoredQueries,
  monitorSql,
  queryDetailSql,
  statusLabel,
  type MonitoredQuery,
} from "../../src/lib/sqlMonitor";

describe("SQL Monitor", () => {
  it("follows one execution without the monitor's time cutoff", () => {
    const id = "q_vliknitwm53b6hxtj2plxwch6dnemism";
    const sql = queryDetailSql("instr", "main", id);
    expect(sql).toContain(`execution_id = '${id}'`);
    expect(sql).not.toContain("INTERVAL");
    expect(sql).toContain("AS is_monitoring");
    expect(() => queryDetailSql("instr", "main", "invalid' OR true")).toThrow(/invalid execution ID/);
  });
  it("keeps active work outside the completion-time window and the history cap", () => {
    const sql = monitorSql("instr", "main", 10, false);
    expect(sql).toContain("FROM hardcharger_live_queries()");
    expect(sql).toContain("status IN ('queued', 'executing', 'fetching') OR COALESCE(cursor_close_time");
    expect(sql).toContain("ORDER BY priority DESC");
    expect(sql).toContain("WHERE instance_id = HARDCHARGER_CURRENT_INSTANCE()::UUID");
    expect(sql).toContain("ORDER BY cursor_close_time DESC LIMIT 1001),");
    expect(sql).toContain("INTERVAL '10 minutes'");
    expect(sql).toContain("'$.page'");
    expect(monitorSql('my"catalog', "main", 30, true)).toContain('"my""catalog"."main".execution_details');
    expect(() => monitorSql("i", "s", NaN, false)).toThrow();
    expect(() => monitorSql("i", "s", 0, false)).toThrow();
  });
  it("filters internal traffic before the history cap and preserves direct detail links", () => {
    const sql = monitorSql("instr", "main", 10, false);
    expect(sql).toContain("'$.internal'");
    expect(sql.indexOf("'$.internal'")).toBeLessThan(sql.indexOf("LIMIT 1001"));
    expect(sql).not.toContain("regexp_matches");
    expect(sql).not.toContain("EXPORT_STATE");
    expect(sql).toContain("SELECT * EXCLUDE (priority, position)"); // retain searchable tags
    const included = monitorSql("instr", "main", 10, true, 1000, undefined, true);
    expect(included).toContain("AS is_internal");
    expect(included).toContain("AS is_monitoring");
    expect(included).not.toContain("NOT (");
    expect(queryDetailSql("instr", "main", "internal_execution")).not.toContain("NOT (");
  });
  it("labels queue waits and execution timeouts distinctly", () => {
    expect(statusLabel({ status: "timeout", cancel_reason: "queue_timeout" })).toBe("Queue timed out");
    expect(statusLabel({ status: "cancelled", cancel_reason: "timeout" })).toBe("Execution timed out");
    expect(statusLabel({ status: "queued", cancel_reason: null })).toBe("Queued");
    expect(statusLabel({ status: "fetching", cancel_reason: null })).toBe("Fetching");
    for (const status of ["queued", "executing", "fetching"])
      expect(activeQuery({ status } as MonitoredQuery)).toBe(true);
    expect(activeQuery({ status: "success" } as MonitoredQuery)).toBe(false);
  });
});

describe("monitor sorting", () => {
  const query = (execution_id: string, overrides: Partial<MonitoredQuery> = {}): MonitoredQuery => ({
    execution_id,
    statement_id: "s",
    session_id: "s",
    username: "admin",
    sql_text: "SELECT 1",
    status: "success",
    error_message: null,
    cancel_reason: null,
    rows_fetched: 1n,
    execution_start_time: 100,
    execution_end_time: 150,
    enqueue_time: null,
    fetch_start_time: 150,
    fetch_end_time: 200,
    cursor_close_time: 200,
    ...overrides,
  });
  const ids = (rows: MonitoredQuery[]) => rows.map((q) => q.execution_id);
  it("defaults to actual start descending, with queued time as fallback and no active pin", () => {
    const rows = [
      query("old-active", { status: "executing", execution_start_time: 10 }),
      query("recent", { execution_start_time: 300, enqueue_time: 1 }),
      query("queued", { status: "queued", enqueue_time: 200, execution_start_time: 0 }),
    ];
    expect(ids(sortMonitoredQueries(rows, defaultMonitorSort, 400))).toEqual(["recent", "queued", "old-active"]);
    expect(ids(rows)).toEqual(["old-active", "recent", "queued"]);
  });
  it("sorts raw durations, including ongoing work, and toggles longest first", () => {
    const rows = [
      query("short", { cursor_close_time: 110 }),
      query("long", { cursor_close_time: 2100 }),
      query("live", { status: "executing", cursor_close_time: null, fetch_end_time: null }),
    ];
    const sort = nextMonitorSort(defaultMonitorSort, "elapsed");
    expect(sort.direction).toBe("desc");
    expect(ids(sortMonitoredQueries(rows, sort, 1000))).toEqual(["long", "live", "short"]);
    expect(ids(sortMonitoredQueries(rows, nextMonitorSort(sort, "elapsed"), 1000))).toEqual(["short", "live", "long"]);
    expect(monitorElapsed(query("q", { enqueue_time: 50 }), 1000)).toBe(150);
  });
  it("keeps missing values last in both directions and preserves large row counts", () => {
    const rows = [
      query("large", { rows_fetched: 9007199254740993n }),
      query("missing", { rows_fetched: null }),
      query("smaller", { rows_fetched: 9007199254740992n }),
    ];
    expect(ids(sortMonitoredQueries(rows, { key: "rows", direction: "asc" }, 0))).toEqual([
      "smaller",
      "large",
      "missing",
    ]);
    expect(ids(sortMonitoredQueries(rows, { key: "rows", direction: "desc" }, 0))).toEqual([
      "large",
      "smaller",
      "missing",
    ]);
    expect(monitorQueueWait(query("q", { status: "queued", enqueue_time: 50 }), 1000)).toBe(950);
    expect(monitorQueueWait(query("q", { cancel_reason: "queue_timeout", enqueue_time: 50 }), 1000)).toBe(150);
    expect(monitorQueueWait(query("q"), 1000)).toBeNull();
  });
  it("sorts text by displayed labels and breaks ties consistently across refresh order", () => {
    const rows = [
      query("b", { status: "success", username: "Zoe", sql_text: "SELECT 2" }),
      query("a", { status: "error", username: "Amy", sql_text: "SELECT 1" }),
    ];
    for (const key of ["status", "user", "sql"] as const)
      expect(ids(sortMonitoredQueries(rows, { key, direction: "asc" }, 0))).toEqual(["a", "b"]);
    expect(ids(sortMonitoredQueries(rows, defaultMonitorSort, 0))).toEqual(["a", "b"]);
    expect(ids(sortMonitoredQueries([...rows].reverse(), defaultMonitorSort, 0))).toEqual(["a", "b"]);
  });
});
