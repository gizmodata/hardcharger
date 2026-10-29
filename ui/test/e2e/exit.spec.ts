// Leaving the page ends its Chargewire sessions on the server at once (keepalive DELETEs on
// pagehide), instead of leaving them open until their idle timeout: closing the tab, and
// navigating away. A page brought back with Back starts new sessions and works as before.
import { type Connection, connect } from "@gizmodata/chargewire";

import { type HarnessState, harnessState } from "./harness.ts";
import { activeTab, expect, gridRows, runDone, signIn, test, watchPage } from "./fixtures.ts";

let admin: Connection;

test.beforeAll(async () => {
  const { serverUrl, admin: who } = harnessState();
  admin = await connect({ url: serverUrl, ...who });
});
test.afterAll(async () => {
  await admin?.close();
});

/** The server's ids of the user's sessions, as the server lists them now. */
async function sessionsOf(username: string): Promise<string[]> {
  const t = await admin.query("SELECT session_id FROM hardcharger_sessions() WHERE username = ? ORDER BY 1", [
    username,
  ]);
  return t.toArray().map((r) => String((r as { session_id: unknown }).session_id));
}

/** Signs in, runs a query (so the tab has a session of its own) and returns the page's sessions. */
async function openWithSessions(
  page: import("@playwright/test").Page,
  harness: HarnessState,
  before: string[],
): Promise<string[]> {
  await signIn(page, harness.user);
  await runDone(page, "SELECT 42 AS answer");
  await expect.poll(async () => (await gridRows(page))[1]).toEqual(["42"]);
  let mine: string[] = [];
  await expect
    .poll(async () => {
      mine = (await sessionsOf(harness.user.username)).filter((id) => !before.includes(id));
      return mine.length;
    })
    .toBeGreaterThanOrEqual(2); // the page's own and the tab's
  return mine;
}

/** Waits until the server lists none of `ids`. */
async function gone(username: string, ids: string[]): Promise<void> {
  await expect
    .poll(async () => (await sessionsOf(username)).filter((id) => ids.includes(id)), {
      timeout: 5_000,
      intervals: [100],
    })
    .toEqual([]);
}

test("closing the page ends its sessions on the server", async ({ context, harness }) => {
  const before = await sessionsOf(harness.user.username);
  const page = await context.newPage();
  const done = await watchPage(page);
  const mine = await openWithSessions(page, harness, before);
  await page.close({ runBeforeUnload: true });
  await gone(harness.user.username, mine);
  done();
});

test("leaving the page ends its sessions; coming back with Back starts new ones", async ({ page, harness }) => {
  const before = await sessionsOf(harness.user.username);
  const mine = await openWithSessions(page, harness, before);
  await page.goto("/ui/third-party-notices.txt");
  await gone(harness.user.username, mine);

  await page.goBack();
  await expect(page.getByTestId("header-user")).toContainText(harness.user.username);
  await runDone(page, "SELECT 43 AS answer");
  await expect.poll(async () => (await gridRows(page))[1]).toEqual(["43"]);
  const now = (await sessionsOf(harness.user.username)).filter((id) => !before.includes(id));
  expect(now.length).toBeGreaterThanOrEqual(2);
  expect(now.filter((id) => mine.includes(id))).toEqual([]);
});

// With a transaction open the browser asks before the page goes. A user who stays keeps the
// sessions and the transaction; one who leaves ends them.
test("with a transaction open, staying keeps the sessions; leaving ends them", async ({ page, harness }) => {
  const before = await sessionsOf(harness.user.username);
  const mine = await openWithSessions(page, harness, before);
  const tab = activeTab(page);
  await runDone(page, "BEGIN");
  await expect(tab.getByTestId("in-transaction")).toBeVisible();

  let asked = 0;
  let stay = true;
  page.on("dialog", (dialog) => {
    asked++;
    void (stay ? dialog.dismiss() : dialog.accept());
  });
  // Closing the tab: the browser asks, and the user stays.
  await page.close({ runBeforeUnload: true });
  await expect.poll(() => asked).toBe(1);
  expect(page.isClosed()).toBe(false);
  await page.waitForTimeout(1_000);
  expect((await sessionsOf(harness.user.username)).filter((id) => mine.includes(id))).toEqual(mine);
  await runDone(page, "SELECT 44 AS answer");
  await expect.poll(async () => (await gridRows(page))[1]).toEqual(["44"]);
  await expect(tab.getByTestId("in-transaction")).toBeVisible();

  stay = false;
  await page.goto("/ui/third-party-notices.txt");
  expect(asked).toBe(2);
  await gone(harness.user.username, mine);
});
