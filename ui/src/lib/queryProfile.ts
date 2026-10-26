// DuckDB 2.0's captured JSON, grouped metrics, and operator tree. The profile text
// is kept verbatim for inspection/download; display values use the server's units.
// https://duckdb.org/docs/2.0/dev/metrics
import { quoteIdentifier, quoteLiteral } from "./sql";

export interface ProfileMetric {
  name: string;
  value: unknown;
  unit?: string;
}
export interface ProfileOperator {
  name: string;
  depth: number;
  timing?: number;
  rows?: number;
  estimatedRows?: string;
  scanned?: number;
  rowGroups?: number;
  totalRowGroups?: number;
  metrics: ProfileMetric[];
  extra: Record<string, unknown>;
}
export interface ParsedProfile {
  groups: { name: string; metrics: ProfileMetric[] }[];
  operators: ProfileOperator[];
  operatorsTruncated: boolean;
}
const object = (value: unknown): Record<string, unknown> | undefined =>
  value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : undefined;
const number = (value: unknown): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined;

export function parseProfile(raw: string, units: ReadonlyMap<string, string>): ParsedProfile {
  const profile = object(JSON.parse(raw) as unknown);
  if (!profile) throw new Error("Expected a JSON profile object");
  const metrics = (group: string, values: Record<string, unknown>): ProfileMetric[] =>
    Object.entries(values)
      .filter(([key]) => !["children", "extra_info", "type", "sql"].includes(key))
      .map(([key, value]) => {
        const name = `${group}.${key}`;
        const unit = units.get(name);
        return { name, value, ...(unit ? { unit } : {}) };
      });
  const groups = Object.entries(profile).flatMap(([name, value]) => {
    const values = object(value);
    return name !== "operator" && values ? [{ name, metrics: metrics(name, values) }] : [];
  });
  const operators: ProfileOperator[] = [];
  const stack = (Array.isArray(profile["operator"]) ? (profile["operator"] as unknown[]) : [])
    .map((value) => ({ value, depth: 0 }))
    .reverse();
  while (stack.length && operators.length < 5000) {
    const { value, depth } = stack.pop()!;
    const node = object(value);
    if (!node) continue;
    const extra = object(node["extra_info"]) ?? {};
    const estimated = extra["Estimated Cardinality"];
    const timing = number(node["timing"]),
      rows = number(node["intermediate_rows"]),
      scanned = number(node["rows_scanned"]);
    const rowGroups = number(node["row_groups_scanned"]),
      totalRowGroups = number(node["total_row_groups_to_scan"]);
    operators.push({
      name: typeof node["type"] === "string" ? node["type"] : "Operator",
      depth,
      ...(timing !== undefined ? { timing } : {}),
      ...(rows !== undefined ? { rows } : {}),
      ...(scanned !== undefined ? { scanned } : {}),
      ...(rowGroups !== undefined ? { rowGroups } : {}),
      ...(totalRowGroups !== undefined ? { totalRowGroups } : {}),
      ...(typeof estimated === "string" || typeof estimated === "number" ? { estimatedRows: String(estimated) } : {}),
      metrics: metrics("operator", node),
      extra,
    });
    if (Array.isArray(node["children"]))
      for (const child of [...(node["children"] as unknown[])].reverse())
        stack.push({ value: child, depth: depth + 1 });
  }
  return { groups, operators, operatorsTruncated: stack.length > 0 };
}

/** Missing metrics stay absent: zero is a real measurement, not a missing value. */
export function profileMetricText(value: unknown, unit?: string): string {
  if (typeof value !== "number" || !Number.isFinite(value))
    return typeof value === "string" ? value : (JSON.stringify(value) ?? "—");
  if (unit === "seconds") {
    const ms = value * 1000;
    return Math.abs(ms) < 1000
      ? `${ms.toLocaleString(undefined, { maximumFractionDigits: 3 })} ms`
      : `${value.toLocaleString(undefined, { maximumFractionDigits: 3 })} s`;
  }
  if (unit === "bytes") {
    if (Math.abs(value) < 1024) return `${value.toLocaleString()} B`;
    const power = Math.min(4, Math.floor(Math.log2(Math.abs(value)) / 10));
    return `${(value / 1024 ** power).toLocaleString(undefined, { maximumFractionDigits: 2 })} ${["B", "KiB", "MiB", "GiB", "TiB"][power]}`;
  }
  return `${value.toLocaleString(undefined, { maximumFractionDigits: 6 })}${unit && unit !== "absolute" ? ` ${unit.replaceAll("_", " ")}` : ""}`;
}

export function profileSql(catalog: string, schema: string, executionId: string): string {
  return `SELECT CAST(query_profile AS VARCHAR) AS profile, cursor_close_time IS NOT NULL AS finished
    FROM ${quoteIdentifier(catalog)}.${quoteIdentifier(schema)}.execution_details
    WHERE instance_id = HARDCHARGER_CURRENT_INSTANCE()::UUID AND execution_id = ${quoteLiteral(executionId)} LIMIT 1`;
}
