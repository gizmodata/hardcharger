import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { parseProfile, profileMetricText, profileSql } from "../../src/lib/queryProfile";

const raw = readFileSync(path.join(import.meta.dirname, "fixtures/query-profile-detailed.json"), "utf8");
const units = new Map([
  ["query.total_time", "seconds"],
  ["operator.timing", "seconds"],
  ["system.peak_buffer_memory", "bytes"],
]);

describe("captured DuckDB 2.0 query profiles", () => {
  it("reads a real detailed profile's grouped metrics and nested operators", () => {
    const profile = parseProfile(raw, units);
    expect(profile.operators.map((op) => op.name)).toEqual([
      "RESULT_COLLECTOR",
      "UNGROUPED_AGGREGATE",
      "PROJECTION",
      "FILTER",
      "TABLE_SCAN",
    ]);
    expect(profile.operators.map((op) => op.depth)).toEqual([0, 1, 2, 3, 4]);
    expect(profile.operators[4]).toMatchObject({
      rows: 1_000_000,
      scanned: 0,
      rowGroups: 0,
      totalRowGroups: 0,
      estimatedRows: "1000000",
    });
    expect(profile.groups.find((g) => g.name === "query")?.metrics).toContainEqual({
      name: "query.total_time",
      value: 0.002416584,
      unit: "seconds",
    });
    expect(
      profile.groups
        .find((g) => g.name === "optimizer")
        ?.metrics.some((m) => m.name === "optimizer.expression_rewriter"),
    ).toBe(true);
    expect(profile.operatorsTruncated).toBe(false);
  });
  it("distinguishes missing metrics from actual zeros, and retains unknown metrics", () => {
    const profile = parseProfile(
      '{"query":{"new_metric":7},"operator":[{"type":"FILTER","timing":0,"rows_scanned":0}]}',
      units,
    );
    expect(profile.operators[0]?.timing).toBe(0);
    expect(profile.operators[0]?.rows).toBeUndefined();
    expect(profile.groups[0]?.metrics[0]).toEqual({ name: "query.new_metric", value: 7 });
    expect(profileMetricText(0, "bytes")).toBe("0 B");
    expect(profileMetricText(0.0015, "seconds")).toBe("1.5 ms");
    expect(profileMetricText(1_048_576, "bytes")).toBe("1 MiB");
    expect(() => parseProfile("broken json", units)).toThrow();
    expect(() => parseProfile("null", units)).toThrow();
  });
  it("fetches exactly one execution from stored instrumentation, with quoted identifiers and IDs", () => {
    const sql = profileSql('my"catalog', "main", "q_'example");
    expect(sql).toContain('FROM "my""catalog"."main".execution_details');
    expect(sql).toContain("execution_id = 'q_''example'");
    expect(sql).toContain("instance_id = HARDCHARGER_CURRENT_INSTANCE()::UUID");
    expect(sql).not.toMatch(/EXPLAIN|EXECUTE|PREPARE/);
  });
});
