import { activeTab, expect, expectProblem, runDone, signIn, test } from "./fixtures.ts";

test("a tab can become read-only, locks the choice, and a new session permits writes", async ({
  page,
  harness,
}, testInfo) => {
  await signIn(page, harness.admin);
  const tab = activeTab(page);
  const mode = tab.getByRole("checkbox", { name: "Read-only", exact: true });
  await runDone(page, "SELECT 1 AS n");
  await runDone(page, "BEGIN");
  await expect(mode).toBeDisabled();
  await expect(tab.getByTestId("session-bar")).toContainText("Finish the transaction");
  await runDone(page, "ROLLBACK");
  await expect(mode).toBeEnabled();
  const original = await tab.getByTestId("session-chip").textContent();
  // The checkbox is confirmed asynchronously by the server, not optimistically.
  await mode.click();
  await expect(mode).toBeChecked();
  await expect(mode).toBeDisabled();
  await expect(tab.getByTestId("session-bar")).toContainText("Locked for this session");
  await expect(tab.getByTestId("upload-button")).toBeDisabled();
  await runDone(page, "SELECT 2 AS n");
  await expect(tab.getByTestId("query-error")).toHaveCount(0);
  expectProblem(page, { status: 400, url: /\/statements$/, required: false });
  expectProblem(page, { status: 403, url: /\/statements$/, required: false });
  await runDone(page, "CREATE TABLE readonly_refused (i INTEGER)");
  await expect(tab.getByTestId("query-error")).toContainText(/read.only/i);
  await page.screenshot({ path: testInfo.outputPath("read-only-locked.png") });
  await tab.getByRole("button", { name: "New session", exact: true }).click();
  await expect(mode).toBeEnabled();
  await mode.uncheck();
  await runDone(page, "CREATE TEMP TABLE writable_again (i INTEGER)");
  await expect(tab.getByTestId("query-error")).toHaveCount(0);
  await expect(tab.getByTestId("session-chip")).not.toHaveText(original!);
  await expect(mode).not.toBeChecked();
});

test("read-only selected before the first run is sent in session creation", async ({ page, harness }) => {
  await signIn(page, harness.admin);
  const tab = activeTab(page);
  await tab.getByRole("checkbox", { name: "Read-only", exact: true }).check();
  const created = page.waitForRequest(
    (r) => r.method() === "POST" && r.url().endsWith("/sessions") && r.postDataJSON()?.options?.read_only === true,
  );
  await runDone(page, "SELECT 42 AS n");
  await created;
  await expect(tab.getByRole("checkbox", { name: "Read-only", exact: true })).toBeDisabled();
  await expect(tab.getByTestId("query-error")).toHaveCount(0);
});

test("connection preference opens remote tabs read-only and survives reload", async ({ page, harness }, testInfo) => {
  await signIn(page, harness.admin);
  await page.getByTestId("explorer-add-server").click();
  const dialog = page.getByTestId("add-server-dialog");
  await dialog.getByLabel("Display name", { exact: true }).fill("Read-only analytics");
  await dialog.getByLabel("Host", { exact: true }).fill(harness.remotes.plain.url);
  await dialog.getByLabel("Username", { exact: true }).fill(harness.remotes.plain.admin.username);
  await dialog.getByLabel("Password", { exact: true }).fill(harness.remotes.plain.admin.password);
  await dialog.getByRole("checkbox", { name: "Read-only sessions", exact: true }).check();
  await page.screenshot({ path: testInfo.outputPath("read-only-connection.png") });
  await dialog.getByTestId("add-server-connect").click();
  await expect(dialog).toHaveCount(0);
  await activeTab(page).getByLabel("Connection", { exact: true }).selectOption({ label: "Read-only analytics" });
  await runDone(page, "SELECT count(*) AS n FROM remote");
  await expect(activeTab(page).getByRole("checkbox", { name: "Read-only", exact: true })).toBeDisabled();
  await page.reload();
  await runDone(page, "SELECT count(*) AS n FROM remote");
  await expect(activeTab(page).getByRole("checkbox", { name: "Read-only", exact: true })).toBeChecked();
  await expect(activeTab(page).getByRole("checkbox", { name: "Read-only", exact: true })).toBeDisabled();
  await expect(activeTab(page).getByTestId("query-error")).toHaveCount(0);
});
