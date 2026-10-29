// A tab's session: the transaction badges (from the session document's `transaction`, Chargewire
// SQL §5.2), Commit and Rollback, the pickers locked in a transaction, the session chip,
// New session, cancelling inside a transaction or a script, and a session the server lost.
import type { Page } from "@playwright/test";

import {
  activeTab,
  editorFocused,
  expect,
  expectProblem,
  gridRows,
  pasted,
  runDone,
  runSql,
  setSql,
  signIn,
  test,
} from "./fixtures.ts";

test.beforeEach(async ({ page, harness }) => {
  await signIn(page, harness.admin);
  await editorFocused(page);
});

// The server ends the cancelled statement's stream without its last chunk; browsers log that
// cut as a failed load (see query.spec.ts).
const cutStream =
  /ERR_INCOMPLETE_CHUNKED_ENCODING|Failed to load resource: (The network connection was lost|Connection terminated unexpectedly)/;
const slow = "SELECT sum(a.range * b.range) AS s FROM range(100000000) a, range(100000) b";

async function value(page: Page, sql: string): Promise<string> {
  await runDone(page, sql);
  await expect.poll(async () => (await gridRows(page)).length).toBe(2);
  return (await gridRows(page))[1]![0]!;
}

test("BEGIN shows In transaction and locks the pickers; Commit ends it", async ({ page }) => {
  const tab = activeTab(page);
  await runDone(page, "CREATE OR REPLACE TABLE tx_ui (i INTEGER)");
  await expect(tab.getByTestId("query-message")).toHaveText("Done.");
  await expect(tab.getByLabel("Catalog")).toBeEnabled();
  await runDone(page, "BEGIN");
  await expect(tab.getByTestId("in-transaction")).toBeVisible();
  await expect(tab.getByLabel("Catalog")).toBeDisabled();
  await expect(tab.getByLabel("Schema")).toBeDisabled();
  await expect(page.locator('[data-marker="transaction"]')).toHaveCount(1); // the tab's amber dot
  await runDone(page, "INSERT INTO tx_ui VALUES (1), (2)");
  // A syntax error doesn't abort a DuckDB transaction: still In transaction.
  expectProblem(page, { status: 400, url: /\/statements$/ });
  await runDone(page, "SELEC 1");
  await expect(tab.getByTestId("query-error")).toBeVisible();
  await expect(tab.getByTestId("in-transaction")).toBeVisible();
  await tab.getByRole("button", { name: "Commit" }).click();
  await expect(tab.getByTestId("in-transaction")).toHaveCount(0);
  await expect(tab.getByLabel("Catalog")).toBeEnabled();
  await expect(page.locator('[data-marker="transaction"]')).toHaveCount(0);
  expect(await value(page, "SELECT count(*) FROM tx_ui")).toBe("2");
});

test("a failed statement aborts the transaction until Rollback", async ({ page }) => {
  expectProblem(page, { status: 400, url: /\/statements$/ });
  const tab = activeTab(page);
  await runDone(page, "BEGIN");
  await expect(tab.getByTestId("in-transaction")).toBeVisible();
  await runDone(page, "SELECT * FROM tx_ui_missing_table");
  await expect(tab.getByTestId("transaction-failed")).toHaveText("Transaction failed — Rollback to continue");
  await tab.getByRole("button", { name: "Rollback" }).click();
  await expect(tab.getByTestId("transaction-failed")).toHaveCount(0);
  await expect(tab.getByTestId("in-transaction")).toHaveCount(0);
  expect(await value(page, "SELECT 'usable' AS v")).toBe("usable");
});

test("the session chip names the server's session and copies it; New session starts another", async ({ page }) => {
  const tab = activeTab(page);
  const id = await value(page, "SELECT HARDCHARGER_CURRENT_SESSION()");
  const chip = tab.getByTestId("session-chip");
  // One id since the server took the Chargewire session's (s_...): the chip shows it without "s_".
  expect(id).toMatch(/^s_/);
  await expect(chip).toContainText(id.replace(/^s_/, "").slice(0, 8));
  await chip.click();
  await expect(tab.getByTestId("tab-notice")).toHaveText("Session id copied.");
  expect(await pasted(page)).toBe(id);
  await runDone(page, "CREATE OR REPLACE TEMP TABLE only_here AS SELECT 1 AS v");
  await runDone(page, "BEGIN");
  await expect(tab.getByTestId("in-transaction")).toBeVisible();
  let asked = "";
  page.once("dialog", (d) => {
    asked = d.message();
    void d.accept();
  });
  await tab.getByRole("button", { name: "New session" }).click();
  await expect.poll(() => asked).toContain("The open transaction will be rolled back");
  await expect(tab.getByTestId("tab-notice")).toHaveText("Session ended. The next run starts a new one.");
  await expect(tab.getByTestId("in-transaction")).toHaveCount(0);
  const next = await value(page, "SELECT HARDCHARGER_CURRENT_SESSION()");
  expect(next).not.toBe(id);
  // Temporary tables belonged to the old session.
  expectProblem(page, { status: 400, url: /\/statements$/ });
  await runDone(page, "SELECT * FROM only_here");
  await expect(tab.getByTestId("query-error")).toContainText("only_here");
});

test("the Catalog and Schema pickers set the session's; a USE moves them", async ({ page, harness }) => {
  const tab = activeTab(page);
  await runDone(page, "CREATE SCHEMA IF NOT EXISTS picker_s");
  await expect(tab.getByTestId("query-message")).toHaveText("Done.");
  await expect(tab.getByLabel("Schema").locator("option", { hasText: "picker_s" })).toHaveCount(1);
  await tab.getByLabel("Schema").selectOption("picker_s");
  expect(await value(page, "SELECT current_schema()")).toBe("picker_s");
  await runDone(page, "USE main");
  await expect(tab.getByLabel("Schema")).toHaveValue("main");
  await expect(tab.getByLabel("Catalog")).toHaveValue(harness.catalog);
});

test("Cancel inside a transaction aborts it, and says to roll back", async ({ page }) => {
  expectProblem(page, { console: cutStream, required: false });
  const tab = activeTab(page);
  await runDone(page, "BEGIN");
  await expect(tab.getByTestId("in-transaction")).toBeVisible();
  await runSql(page, slow);
  await expect(tab.getByTestId("cancel-button")).toBeVisible();
  await page.waitForTimeout(500);
  await tab.getByTestId("cancel-button").click();
  await expect(tab.getByTestId("tab-notice")).toContainText("The query was cancelled", { timeout: 15_000 });
  await expect(tab.getByTestId("tab-notice")).toContainText("run Rollback to continue");
  await expect(tab.getByTestId("transaction-failed")).toBeVisible();
  await tab.getByRole("button", { name: "Rollback" }).click();
  await expect(tab.getByTestId("transaction-failed")).toHaveCount(0);
});

test("Cancel in a script says how far it got", async ({ page }) => {
  expectProblem(page, { console: cutStream, required: false });
  const tab = activeTab(page);
  await setSql(page, `SELECT 1 AS before;\n${slow};\nSELECT 3 AS after;`);
  await page.keyboard.press("ControlOrMeta+Shift+Enter");
  await expect(tab.getByTestId("results-status")).toContainText("statement 2 of 3", { timeout: 10_000 });
  await page.waitForTimeout(300);
  await tab.getByTestId("cancel-button").click();
  await expect(tab.getByTestId("tab-notice")).toHaveText(
    "Statement 2 of 3 was cancelled; nothing from it was saved (statement 1 ran, statement 3 did not run).",
    { timeout: 15_000 },
  );
  await tab.getByRole("tab", { name: "Messages" }).click();
  await expect(tab.getByTestId("messages").getByRole("listitem").nth(2)).toContainText("not run");
});

/** Ends the active tab's session behind its back, as an idle timeout or a restart would. */
async function loseSession(page: Page): Promise<void> {
  const id = (await activeTab(page).getByTestId("session-chip").getAttribute("title"))!.match(
    /Chargewire session (s_[A-Za-z0-9]+)/,
  )![1]!;
  const status = await page.evaluate(async (sid) => {
    const saved = JSON.parse(sessionStorage.getItem("hardcharger-ui:sign-in") ?? "{}") as { token?: string };
    const r = await fetch(`/chargewire/v1/sessions/${sid}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${saved.token ?? ""}` },
    });
    return r.status;
  }, id);
  expect(status).toBe(204);
}

test("a session the server lost: a new one takes over, or, with a transaction open, nothing runs", async ({ page }) => {
  // The next request in the lost session is answered session_not_found, and so is the
  // client's own DELETE of it as it lets the connection go.
  expectProblem(page, { status: 404, url: /\/statements$/ });
  expectProblem(page, { status: 404, url: /\/chargewire\/v1\/sessions\/s_[A-Za-z0-9]+$/, required: false });
  const tab = activeTab(page);
  const first = await value(page, "SELECT HARDCHARGER_CURRENT_SESSION()");
  await loseSession(page);
  const second = await value(page, "SELECT HARDCHARGER_CURRENT_SESSION()");
  expect(second).not.toBe(first);
  await expect(tab.getByTestId("tab-notice")).toContainText(
    "A new session was started: the previous one ended (it was idle too long, or the server restarted).",
  );

  await runDone(page, "CREATE OR REPLACE TABLE lost_tx (i INTEGER)");
  await runDone(page, "BEGIN");
  await runDone(page, "INSERT INTO lost_tx VALUES (1)");
  await expect(tab.getByTestId("in-transaction")).toBeVisible();
  await loseSession(page);
  await runDone(page, "INSERT INTO lost_tx VALUES (2)");
  await expect(tab.getByTestId("query-error")).toContainText(
    "so the transaction was rolled back and nothing from this run executed",
  );
  await expect(tab.getByTestId("in-transaction")).toHaveCount(0);
  // Neither insert is there; the next run works on a new session.
  expect(await value(page, "SELECT count(*) FROM lost_tx")).toBe("0");
});

test("leaving the page with a transaction open asks first", async ({ page }) => {
  const tab = activeTab(page);
  await runDone(page, "BEGIN");
  await expect(tab.getByTestId("in-transaction")).toBeVisible();
  let type = "";
  page.once("dialog", (d) => {
    type = d.type();
    void d.dismiss();
  });
  await page.close({ runBeforeUnload: true });
  await expect.poll(() => type).toBe("beforeunload");
});
