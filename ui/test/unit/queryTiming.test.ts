import { describe, expect, it } from "vitest";

import { queryTimings, timingElapsed, timingTimestamp } from "../../src/lib/queryTiming";
import type { MonitoredQuery } from "../../src/lib/sqlMonitor";

const at = new Date(2026, 9, 26, 16, 20, 0).getTime();
const completed: MonitoredQuery = {
  execution_id: "q_timing",
  statement_id: "q_timing",
  session_id: "s_timing",
  username: "admin",
  sql_text: "SELECT 42",
  status: "success",
  error_message: null,
  cancel_reason: null,
  rows_fetched: 1n,
  enqueue_time: at,
  execution_start_time: at + 5,
  execution_end_time: at + 55,
  fetch_start_time: at + 60,
  fetch_end_time: at + 400,
  cursor_close_time: at + 410,
};

describe("query step timings", () => {
  it("separates queue, execution, fetching, cursor release and total, freezing completed times", () => {
    const steps = queryTimings(completed, at + 10_000);
    expect(steps.map((step) => [step.id, step.elapsed])).toEqual([
      ["queue", 5],
      ["execution", 50],
      ["first-batch", 5],
      ["fetch", 340],
      ["cursor", 10],
      ["total", 410],
    ]);
    expect(steps.some((step) => step.live)).toBe(false);
    expect(queryTimings(completed, at + 100_000)).toEqual(steps);
  });

  it("counts live queue wait without claiming that execution has begun", () => {
    const query = {
      ...completed,
      status: "queued",
      execution_start_time: at,
      execution_end_time: null,
      fetch_start_time: null,
      fetch_end_time: null,
      cursor_close_time: null,
    };
    const steps = queryTimings(query, at + 2000);
    expect(steps[0]).toMatchObject({ start: at, end: null, elapsed: 2000, live: true });
    expect(steps[1]).toMatchObject({ start: null, end: null, elapsed: null, live: false });
    expect(steps[5]).toMatchObject({ elapsed: 2000, live: true });
  });

  it.each(["queue_timeout", "client", null])("does not count a queue rejection as execution (%s)", (reason) => {
    const steps = queryTimings(
      {
        ...completed,
        status: reason === "queue_timeout" ? "timeout" : "cancelled",
        cancel_reason: reason,
        execution_start_time: at,
        execution_end_time: at + 3000,
        fetch_start_time: null,
        fetch_end_time: null,
        cursor_close_time: at + 3000,
      },
      at + 90_000,
    );
    expect(steps[0]).toMatchObject({ elapsed: 3000, live: false });
    expect(steps[1]).toMatchObject({ start: null, end: null, elapsed: null });
    expect(steps[5]).toMatchObject({ elapsed: 3000, live: false });
  });

  it("keeps a genuine zero-wait execution and distinguishes execution timeouts", () => {
    const steps = queryTimings(
      { ...completed, execution_start_time: at, status: "timeout", cancel_reason: "timeout" },
      at + 10_000,
    );
    expect(steps[0]!.elapsed).toBe(0);
    expect(steps[1]).toMatchObject({ start: at, elapsed: 55 });
  });

  it("updates only the active execution/fetch and includes outstanding cursor release in total", () => {
    const executing = {
      ...completed,
      enqueue_time: null,
      status: "executing",
      execution_end_time: null,
      fetch_start_time: null,
      fetch_end_time: null,
      cursor_close_time: null,
    };
    expect(queryTimings(executing, at + 1000)[0]).toMatchObject({ elapsed: null, empty: "Not queued" });
    expect(queryTimings(executing, at + 1000)[1]).toMatchObject({ elapsed: 995, live: true });
    const firstBatch = { ...executing, execution_end_time: at + 55 };
    expect(queryTimings(firstBatch, at + 1000)[1]).toMatchObject({ elapsed: 50, live: false });
    expect(queryTimings(firstBatch, at + 1000)[2]).toMatchObject({ elapsed: 945, live: true });
    const fetching = { ...completed, status: "fetching", fetch_end_time: null, cursor_close_time: null };
    expect(queryTimings(fetching, at + 1000)[1]).toMatchObject({ elapsed: 50, live: false });
    expect(queryTimings(fetching, at + 1000)[3]).toMatchObject({ elapsed: 940, live: true });
    expect(queryTimings({ ...completed, cursor_close_time: null }, at + 1000)[4]).toMatchObject({
      elapsed: 600,
      live: true,
    });
    expect(queryTimings({ ...completed, cursor_close_time: null }, at + 1000)[5]).toMatchObject({
      elapsed: 1000,
      live: true,
    });
  });

  it("leaves elapsed unknown when no first batch was fetched", () => {
    expect(queryTimings({ ...completed, fetch_start_time: null }, at)[3]!.elapsed).toBeNull();
  });

  it("keeps missing times distinct from zero and formats sub-ms and long durations", () => {
    expect(timingElapsed(null)).toBe("—");
    expect(timingElapsed(0)).toBe("0 ms");
    expect(timingElapsed(0.25)).toBe("0.25 ms");
    expect(timingElapsed(0.001)).toBe("<0.01 ms");
    expect(timingElapsed(-10)).toBe("0 ms");
    expect(timingElapsed(1234)).toBe("1.23 s");
    expect(timingElapsed(62_000)).toBe("1 min 2 s");
    expect(timingElapsed(3_662_000)).toBe("1 h 1 min 2 s");
  });
});

describe("local timeline timestamps", () => {
  it("shows a same-day end time without repeating its date", () => {
    expect(timingTimestamp(at + 1000, at, "en-US")).toBe(new Date(at + 1000).toLocaleTimeString("en-US"));
    expect(timingTimestamp(at, null, "en-US")).toBe(new Date(at).toLocaleString("en-US"));
    expect(timingTimestamp(null, at)).toBe("—");
  });

  it("keeps the end date across midnight, month and year boundaries", () => {
    for (const [start, end] of [
      [new Date(2026, 9, 26, 23, 59), new Date(2026, 9, 27, 0, 1)],
      [new Date(2026, 9, 26, 16), new Date(2026, 10, 26, 16)],
      [new Date(2026, 9, 26, 16), new Date(2027, 9, 26, 16)],
    ] as const)
      expect(timingTimestamp(end.getTime(), start.getTime(), "en-US")).toBe(end.toLocaleString("en-US"));
  });
});
