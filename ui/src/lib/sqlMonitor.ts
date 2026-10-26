import { quoteIdentifier } from "./sql";

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
  execution_id: string;
  statement_id: string;
  session_id: string;
  username: string;
  sql_text: string;
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
): string {
  if (!Number.isInteger(minutes) || minutes < 1 || minutes > 1440)
    throw new Error("Choose between 1 minute and 24 hours");
  if (!Number.isInteger(historyLimit) || historyLimit < 1 || historyLimit > 1_000_000)
    throw new Error("Invalid history row limit");
  const history = `${quoteIdentifier(catalog)}.${quoteIdentifier(schema)}.execution_details`;
  const columns =
    "execution_id, statement_id, session_id, username, sql_text, status, error_message, cancel_reason, rows_fetched, execution_start_time, execution_end_time, enqueue_time, fetch_start_time, fetch_end_time, cursor_close_time, CAST(query_tag AS VARCHAR) AS query_tag";
  const recent = `COALESCE(cursor_close_time, fetch_end_time, execution_end_time) >= current_timestamp - INTERVAL '${minutes} minutes'`;
  const visible = includeMonitor
    ? "true"
    : "COALESCE(json_extract_string(NULLIF(CAST(query_tag AS VARCHAR), ''), '$.page'), '') <> 'sql-monitor'";
  return `WITH live AS (SELECT ${columns}, 1 AS priority FROM hardcharger_live_queries()),
    history AS (SELECT ${columns}, 0 AS priority FROM ${history}
      WHERE instance_id = HARDCHARGER_CURRENT_INSTANCE()::UUID AND cursor_close_time IS NOT NULL
        AND ${recent} AND ${visible}
      ORDER BY cursor_close_time DESC LIMIT ${historyLimit + 1}),
    merged AS (SELECT * FROM live UNION ALL SELECT * FROM history),
    ranked AS (SELECT *, row_number() OVER (PARTITION BY execution_id ORDER BY priority DESC) AS position FROM merged)
    SELECT * EXCLUDE (priority, position, query_tag) FROM ranked
    WHERE position = 1 AND ${visible} AND (status IN ('queued', 'executing', 'fetching') OR ${recent})
    ORDER BY status IN ('queued', 'executing', 'fetching') DESC, COALESCE(enqueue_time, execution_start_time) DESC`;
}
