import type { MonitoredQuery } from "./sqlMonitor";

export interface QueryTiming {
  id: string;
  label: string;
  start: number | null;
  end: number | null;
  elapsed: number | null;
  live: boolean;
  empty: string;
}

/** Instrumentation's execution clock starts on admission, not at enqueue. */
export function queryTimings(query: MonitoredQuery, now: number): QueryTiming[] {
  // Refused/cancelled queue entries retain enqueue_time as a placeholder start.
  // Real completed executions always record fetch_end, even without result batches.
  const neverExecuted =
    query.status === "queued" ||
    (query.enqueue_time !== null &&
      query.execution_start_time === query.enqueue_time &&
      query.fetch_start_time === null &&
      query.fetch_end_time === null &&
      query.cursor_close_time !== null);
  const step = (
    id: string,
    label: string,
    start: number | null,
    end: number | null,
    live: boolean,
    empty = "Not started",
  ): QueryTiming => ({
    id,
    label,
    start,
    end,
    live: live && start !== null && end === null,
    elapsed: start === null || (end === null && !live) ? null : Math.max(0, (end ?? now) - start),
    empty,
  });
  const executionStart = neverExecuted ? null : query.execution_start_time;
  return [
    step(
      "queue",
      "Queue wait",
      query.enqueue_time,
      query.enqueue_time === null
        ? null
        : neverExecuted
          ? (query.cursor_close_time ?? query.execution_end_time)
          : executionStart,
      query.status === "queued",
      query.enqueue_time === null ? "Not queued" : "Not recorded",
    ),
    step(
      "execution",
      "Execution / result cursor",
      executionStart,
      neverExecuted ? null : query.execution_end_time,
      query.status === "executing",
      neverExecuted ? "Not started" : "Not recorded",
    ),
    step(
      "first-batch",
      "Fetch setup / first batch",
      neverExecuted ? null : query.execution_end_time,
      query.fetch_start_time ?? query.fetch_end_time,
      !neverExecuted && query.cursor_close_time === null,
    ),
    step("fetch", "Fetch results", query.fetch_start_time, query.fetch_end_time, query.status === "fetching"),
    step("cursor", "Release cursor", query.fetch_end_time, query.cursor_close_time, query.fetch_end_time !== null),
    step("total", "Total", query.enqueue_time ?? query.execution_start_time, query.cursor_close_time, true),
  ];
}

/** Omit the repeated date only when both instants fall on the same local calendar day. */
export function timingTimestamp(ms: number | null, start: number | null = null, locale?: string): string {
  if (ms === null) return "—";
  const date = new Date(ms);
  const first = start === null ? undefined : new Date(start);
  return first &&
    first.getFullYear() === date.getFullYear() &&
    first.getMonth() === date.getMonth() &&
    first.getDate() === date.getDate()
    ? date.toLocaleTimeString(locale)
    : date.toLocaleString(locale);
}

export function timingElapsed(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms)) return "—";
  const value = Math.max(0, ms);
  if (value > 0 && value < 0.01) return "<0.01 ms";
  if (value < 1000) return `${Number(value.toFixed(2))} ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(2)} s`;
  const seconds = Math.floor(value / 1000);
  const minutes = Math.floor(seconds / 60);
  return minutes < 60
    ? `${minutes} min ${seconds % 60} s`
    : `${Math.floor(minutes / 60)} h ${minutes % 60} min ${seconds % 60} s`;
}
