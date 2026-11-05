import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clock, describeResume, resumeWaitMs, sendWhileResuming, type ResumeProgress } from "../../src/lib/resuming";

function resuming(retryAfter: string | null, detail = "the server is resuming; retry after 2 s"): Response {
  const headers: Record<string, string> = { "Content-Type": "application/problem+json" };
  if (retryAfter !== null) headers["Retry-After"] = retryAfter;
  return new Response(JSON.stringify({ code: "server_resuming", status: 503, detail }), { status: 503, headers });
}
const ok = () => new Response(JSON.stringify({ access_token: "t" }), { status: 200 });

describe("resumeWaitMs (Chargewire §11.2)", () => {
  it("waits Retry-After, 3 s without one, at least 1 s and at most 60 s", () => {
    expect(resumeWaitMs("7")).toBe(7000);
    expect(resumeWaitMs(" 12 ")).toBe(12_000);
    expect(resumeWaitMs(null)).toBe(3000);
    expect(resumeWaitMs("")).toBe(3000);
    expect(resumeWaitMs("Wed, 21 Oct 2026 07:28:00 GMT")).toBe(3000);
    expect(resumeWaitMs("-1")).toBe(3000);
    expect(resumeWaitMs("1.5")).toBe(3000);
    expect(resumeWaitMs("0")).toBe(1000);
    expect(resumeWaitMs("120")).toBe(60_000);
    expect(resumeWaitMs("99999999999999")).toBe(3000);
  });
});

describe("sendWhileResuming", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("sends the request again as the server asks, telling each wait, then returns the answer", async () => {
    const answers = [resuming("2"), resuming(null), ok()];
    const send = vi.fn(() => Promise.resolve(answers.shift()!));
    const heard: ResumeProgress[] = [];
    const done = sendWhileResuming(send, { onResuming: (p) => heard.push(p) });
    await vi.advanceTimersByTimeAsync(2000);
    await vi.advanceTimersByTimeAsync(3000);
    const resp = await done;
    expect(resp.status).toBe(200);
    expect(send).toHaveBeenCalledTimes(3);
    expect(heard.map((p) => [p.attempt, p.retryAfterMs, p.elapsedMs, p.resumeTimeoutMs])).toEqual([
      [1, 2000, 0, 600_000],
      [2, 3000, 2000, 600_000],
    ]);
    expect(heard[0]!.detail).toBe("the server is resuming; retry after 2 s");
  });

  it("returns the server's answer once the resume timeout would pass", async () => {
    const send = vi.fn(() => Promise.resolve(resuming("2")));
    const done = sendWhileResuming(send, { resumeTimeoutS: 5 });
    await vi.advanceTimersByTimeAsync(10_000);
    const resp = await done;
    expect(resp.status).toBe(503);
    // Answers at 0, 2 and 4 s; waiting 2 s more would end past 5 s.
    expect(send).toHaveBeenCalledTimes(3);
  });

  it("with a resume timeout of 0 reports the first answer", async () => {
    const send = vi.fn(() => Promise.resolve(resuming("1")));
    const onResuming = vi.fn();
    const resp = await sendWhileResuming(send, { resumeTimeoutS: 0, onResuming });
    expect(resp.status).toBe(503);
    expect(send).toHaveBeenCalledTimes(1);
    expect(onResuming).not.toHaveBeenCalled();
  });

  it("passes any other answer through at once, a 503 that isn't server_resuming included", async () => {
    const other = new Response(JSON.stringify({ code: "service_unavailable" }), { status: 503 });
    const send = vi.fn(() => Promise.resolve(other));
    expect(await sendWhileResuming(send)).toBe(other);
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("stops a wait at once when aborted", async () => {
    const send = vi.fn(() => Promise.resolve(resuming("60")));
    const stop = new AbortController();
    const done = sendWhileResuming(send, { signal: stop.signal });
    const caught = done.catch((err: unknown) => err);
    await vi.advanceTimersByTimeAsync(10);
    stop.abort();
    const err = await caught;
    expect((err as DOMException).name).toBe("AbortError");
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("ignores a progress display that throws", async () => {
    const answers = [resuming("1"), ok()];
    const send = vi.fn(() => Promise.resolve(answers.shift()!));
    const done = sendWhileResuming(send, {
      onResuming: () => {
        throw new Error("display broke");
      },
    });
    await vi.advanceTimersByTimeAsync(1000);
    expect((await done).status).toBe(200);
  });
});

describe("describeResume", () => {
  it("counts down to the next attempt and up from the first answer", () => {
    const p: ResumeProgress = {
      attempt: 2,
      retryAfterMs: 5000,
      elapsedMs: 61_000,
      resumeTimeoutMs: 600_000,
      detail: "the server is resuming; retry after 5 s",
      at: 1_000_000,
    };
    expect(describeResume(p, 1_000_000)).toEqual({
      retryInS: 5,
      sofar: "1:01",
      text: "The server is resuming: retrying in 5 s, 1:01 so far.",
    });
    expect(describeResume(p, 1_003_200).retryInS).toBe(2);
    expect(describeResume(p, 1_003_200).sofar).toBe("1:04");
    expect(describeResume(p, 1_009_000).retryInS).toBe(0);
    // A clock a tick behind the report shows the report's own numbers, not more.
    expect(describeResume(p, 999_400)).toEqual(describeResume(p, 1_000_000));
    // A holder's own words are shown; the spec's stock detail only restates the sentence.
    expect(describeResume({ ...p, detail: "cluster warehouse is starting, about 40 s left" }, 1_000_000).text).toBe(
      "The server is resuming (cluster warehouse is starting, about 40 s left): retrying in 5 s, 1:01 so far.",
    );
    expect(describeResume({ ...p, detail: "the server is resuming" }, 1_000_000).text).toBe(
      "The server is resuming: retrying in 5 s, 1:01 so far.",
    );
    expect(clock(0)).toBe("0:00");
    expect(clock(3_725_000)).toBe("62:05");
  });
});
