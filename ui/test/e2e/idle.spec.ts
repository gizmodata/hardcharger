// A session the server itself ended for being idle (not one the test deleted): on a server of
// its own whose sessions end after 2 s idle. With no transaction open the next run starts a
// new session and says so; with one open the run is refused, nothing of it executes, and the
// run after that works. The schema browser, which has a session of its own, carries on too.
import { connect } from "@gizmodata/warp-sql";

import { type IdleServer, startIdleServer } from "./harness.ts";
import { activeTab, editorFocused, expect, expectProblem, gridRows, runDone, signIn, test } from "./fixtures.ts";
import type { Page } from "@playwright/test";

const idleSeconds = 2;
let idle: IdleServer;

test.beforeAll(async () => {
  idle = await startIdleServer(idleSeconds);
});
test.afterAll(async () => {
  await idle?.stop();
});

async function value(page: Page, sql: string): Promise<string> {
  await runDone(page, sql);
  await expect.poll(async () => (await gridRows(page)).length).toBe(2);
  return (await gridRows(page))[1]![0]!;
}

/** Waits until the server no longer lists the session (its sweep runs every 5 s). */
async function evicted(sessionId: string): Promise<void> {
  const admin = await connect({ url: idle.url, ...idle.admin });
  try {
    await expect
      .poll(
        async () => {
          const t = await admin.query(
            "SELECT count(*)::INTEGER AS n FROM hardcharger_sessions() WHERE session_id = ?",
            [sessionId],
          );
          return Number(t.get(0)!.toJSON()["n"]);
        },
        { timeout: 30_000, intervals: [500] },
      )
      .toBe(0);
  } finally {
    await admin.close();
  }
}

test("a session the server ended for idling: a new one takes over, or, in a transaction, the run is refused", async ({
  page,
}) => {
  // The lost session's next request is answered session_not_found (404), and so is the
  // client's DELETE of it; the page's own session and the browser's are replaced the same way.
  expectProblem(page, { status: 404, url: /\/statements$/ });
  expectProblem(page, { status: 404, url: /\/warp\/v1\/sessions\/s_[A-Za-z0-9]+(\/.*)?$/, required: false });
  await signIn(page, idle.admin, idle.url);
  await editorFocused(page);
  const tab = activeTab(page);

  const first = await value(page, "SELECT HARDCHARGER_CURRENT_SESSION()");
  await evicted(first);
  const second = await value(page, "SELECT HARDCHARGER_CURRENT_SESSION()");
  expect(second).not.toBe(first);
  await expect(tab.getByTestId("tab-notice")).toContainText(
    "A new session was started: the previous one ended (it was idle too long, or the server restarted).",
  );

  await runDone(page, "CREATE OR REPLACE TABLE idle_tx (i INTEGER)");
  await runDone(page, "BEGIN");
  await runDone(page, "INSERT INTO idle_tx VALUES (1)");
  await expect(tab.getByTestId("in-transaction")).toBeVisible();
  await evicted(second);
  await runDone(page, "INSERT INTO idle_tx VALUES (2)");
  await expect(tab.getByTestId("query-error")).toContainText(
    "so the transaction was rolled back and nothing from this run executed",
  );
  await expect(tab.getByTestId("in-transaction")).toHaveCount(0);
  // Neither insert is there; the next run works, on a new session.
  expect(await value(page, "SELECT count(*) FROM idle_tx")).toBe("0");

  // The schema browser lists through a session of its own, which idled out as well.
  await page.getByRole("tab", { name: "Data" }).click();
  await page.getByRole("button", { name: "Refresh the schema browser" }).click();
  await expect(page.getByRole("treeitem", { name: /^e2e/ }).first()).toBeVisible();
});

test("the help names this server's idle timeout", async ({ page }) => {
  await signIn(page, idle.admin, idle.url);
  await page.getByRole("button", { name: "How the editor works" }).click();
  await expect(page.getByTestId("help-body")).toContainText(
    `ends after ${idleSeconds} seconds idle (this server's setting)`,
  );
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("help-body")).toHaveCount(0);
});
