import { describe, expect, it } from "vitest";
import { activeQuery, monitorSql, queryDetailSql, statusLabel, type MonitoredQuery } from "../../src/lib/sqlMonitor";

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
