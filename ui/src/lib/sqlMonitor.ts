import { quoteIdentifier, quoteLiteral } from "./sql";

export interface MonitorInfo {
  instrumentation_enabled: boolean;
  queueing_enabled: boolean;
  concurrency_limit: bigint;
  slots_in_use: bigint;
  slots_available: bigint | null;
  queued_statements: bigint;
  queue_capacity: bigint;
  max_queue_wait_seconds: bigint;
  admin_bypass_default: boolean;
  history_backlog: bigint;
  history_dropped: bigint;
  history_failed: bigint;
}

export interface MonitoredQuery {
  is_internal?: boolean;
  is_monitoring?: boolean;
  execution_id: string;
  statement_id: string;
  session_id: string;
  username: string;
  sql_text: string;
  query_tag?: string | null;
  status: string;
  error_message: string | null;
  cancel_reason: string | null;
  rows_fetched: bigint | null;
  execution_start_time: number;
  execution_end_time: number | null;
  enqueue_time: number | null;
  fetch_start_time: number | null;
  fetch_end_time: number | null;
  cursor_close_time: number | null;
}

export const activeQuery = (query: MonitoredQuery): boolean =>
  ["queued", "executing", "fetching"].includes(query.status);

export type MonitorSortKey = "status" | "user" | "sql" | "started" | "queueWait" | "elapsed" | "rows";
export interface MonitorSort {
  key: MonitorSortKey;
  direction: "asc" | "desc";
}
export const defaultMonitorSort: MonitorSort = { key: "started", direction: "desc" };

export const monitorStartTime = (q: MonitoredQuery): number =>
  q.status === "queued" ? (q.enqueue_time ?? q.execution_start_time) : q.execution_start_time;

export const monitorElapsed = (q: MonitoredQuery, now: number): number =>
  Math.max(0, (q.cursor_close_time ?? q.fetch_end_time ?? now) - (q.enqueue_time ?? q.execution_start_time));

export const monitorQueueWait = (q: MonitoredQuery, now: number): number | null =>
  q.enqueue_time == null
    ? null
    : Math.max(
        0,
        (q.status === "queued"
          ? now
          : q.cancel_reason === "queue_timeout"
            ? (q.cursor_close_time ?? q.execution_end_time ?? now)
            : q.execution_start_time) - q.enqueue_time,
      );

export function nextMonitorSort(current: MonitorSort, key: MonitorSortKey): MonitorSort {
  return {
    key,
    direction:
      current.key === key
        ? current.direction === "asc"
          ? "desc"
          : "asc"
        : ["status", "user", "sql"].includes(key)
          ? "asc"
          : "desc",
  };
}

/** Sort the loaded sample using the same raw values as the displayed cells. */
export function sortMonitoredQueries(
  queries: readonly MonitoredQuery[],
  sort: MonitorSort,
  now: number,
): MonitoredQuery[] {
  const value = (q: MonitoredQuery) => {
    switch (sort.key) {
      case "status":
        return statusLabel(q);
      case "user":
        return q.username;
      case "sql":
        return q.sql_text;
      case "started":
        return monitorStartTime(q);
      case "queueWait":
        return monitorQueueWait(q, now);
      case "elapsed":
        return monitorElapsed(q, now);
      case "rows":
        return q.rows_fetched;
    }
  };
  return [...queries].sort((a, b) => {
    const av = value(a);
    const bv = value(b);
    // Missing values stay last in either direction; retain bigint precision.
    if (av == null && bv != null) return 1;
    if (bv == null && av != null) return -1;
    const comparison =
      av == null || bv == null
        ? 0
        : typeof av === "string" && typeof bv === "string"
          ? av.localeCompare(bv)
          : av < bv
            ? -1
            : av > bv
              ? 1
              : 0;
    return (
      comparison * (sort.direction === "asc" ? 1 : -1) ||
      monitorStartTime(b) - monitorStartTime(a) ||
      a.execution_id.localeCompare(b.execution_id)
    );
  });
}

export function statusLabel(query: Pick<MonitoredQuery, "status" | "cancel_reason">): string {
  if (query.cancel_reason === "queue_timeout") return "Queue timed out";
  if (query.cancel_reason === "timeout" || query.status === "timeout") return "Execution timed out";
  return (
    (
      {
        queued: "Queued",
        executing: "Executing",
        fetching: "Fetching",
        success: "Succeeded",
        error: "Failed",
        cancelled: "Cancelled",
      } as Record<string, string>
    )[query.status] ?? query.status
  );
}

/** Recent completions, plus EVERY live statement, with live state winning over
 * asynchronously written history. The history cap never limits active statements. */
export function monitorSql(
  catalog: string,
  schema: string,
  minutes: number,
  includeMonitor: boolean,
  historyLimit = 1000,
  executionId?: string,
  includeInternal = false,
): string {
  if (!Number.isInteger(minutes) || minutes < 1 || minutes > 1440)
    throw new Error("Choose between 1 minute and 24 hours");
  if (!Number.isInteger(historyLimit) || historyLimit < 1 || historyLimit > 1_000_000)
    throw new Error("Invalid history row limit");
  const history = `${quoteIdentifier(catalog)}.${quoteIdentifier(schema)}.execution_details`;
  const columns =
    "execution_id, statement_id, session_id, username, sql_text, status, error_message, cancel_reason, rows_fetched, execution_start_time, execution_end_time, enqueue_time, fetch_start_time, fetch_end_time, cursor_close_time, CAST(query_tag AS VARCHAR) AS query_tag";
  const recent = executionId
    ? "true"
    : `COALESCE(cursor_close_time, fetch_end_time, execution_end_time) >= current_timestamp - INTERVAL '${minutes} minutes'`;
  const execution = executionId ? `execution_id = ${quoteLiteral(executionId)}` : "true";
  const tag = "NULLIF(CAST(query_tag AS VARCHAR), '')";
  const monitorMatch = `COALESCE(json_extract_string(${tag}, '$.monitoring'), 'false') = 'true' OR COALESCE(json_extract_string(${tag}, '$.page'), '') = 'sql-monitor'`;
  // Explicit producer tags, never SQL-text guesses. Apply before the history cap too.
  const internalMatch = `COALESCE(json_extract_string(${tag}, '$.internal'), 'false') = 'true'`;
  // These are additive categories: either checkbox reveals a query tagged as both.
  const categories = [`(NOT (${internalMatch}) AND NOT (${monitorMatch}))`];
  if (includeInternal) categories.push(`(${internalMatch})`);
  if (includeMonitor) categories.push(`(${monitorMatch})`);
  const visible = includeInternal && includeMonitor ? "true" : `(${categories.join(" OR ")})`;
  return `WITH live AS (SELECT ${columns}, 1 AS priority FROM hardcharger_live_queries() WHERE ${execution}),
    history AS (SELECT ${columns}, 0 AS priority FROM ${history}
      WHERE instance_id = HARDCHARGER_CURRENT_INSTANCE()::UUID AND cursor_close_time IS NOT NULL
        AND ${execution} AND ${recent} AND ${visible}
      ORDER BY cursor_close_time DESC LIMIT ${historyLimit + 1}),
    merged AS (SELECT * FROM live UNION ALL SELECT * FROM history),
    ranked AS (SELECT *, row_number() OVER (PARTITION BY execution_id ORDER BY priority DESC) AS position FROM merged)
    SELECT * EXCLUDE (priority, position), (${internalMatch}) AS is_internal, (${monitorMatch}) AS is_monitoring FROM ranked
    WHERE position = 1 AND ${visible} AND (status IN ('queued', 'executing', 'fetching') OR ${recent})
    ORDER BY status IN ('queued', 'executing', 'fetching') DESC, COALESCE(enqueue_time, execution_start_time) DESC`;
}

export function queryDetailSql(catalog: string, schema: string, executionId: string): string {
  if (!/^[a-zA-Z0-9_-]{1,128}$/.test(executionId))
    throw new Error("This query detail link has an invalid execution ID.");
  return monitorSql(catalog, schema, 10, true, 1, executionId, true);
}

export type DetailDisplay = "window" | "inline";
const detailPreferenceKey = "hardcharger-ui:monitor-detail-display";
export function storedDetailDisplay(): DetailDisplay {
  try {
    return localStorage.getItem(detailPreferenceKey) === "inline" ? "inline" : "window";
  } catch {
    return "window";
  }
}
export function saveDetailDisplay(value: DetailDisplay) {
  try {
    localStorage.setItem(detailPreferenceKey, value);
  } catch {
    /* Storage is optional. */
  }
}

export type StatusDisplay = "icons" | "descriptions";
const preferenceKey = "hardcharger-ui:monitor-status-display";
export function storedStatusDisplay(): StatusDisplay {
  try {
    return localStorage.getItem(preferenceKey) === "descriptions" ? "descriptions" : "icons";
  } catch {
    return "icons";
  }
}
export function saveStatusDisplay(value: StatusDisplay) {
  try {
    localStorage.setItem(preferenceKey, value);
  } catch {
    /* Preference is session-only if storage is blocked. */
  }
}
