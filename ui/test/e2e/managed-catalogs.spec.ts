// The server's own catalogs (catalog logging's _hardcharger_logs, the instrumentation one): an
// administrator reads them by name, but the server never makes one a session's catalog. A
// preview of one of their tables runs (its query names the catalog) in the tab's own catalog,
// saying why; asking a tab to use such a catalog is refused with that note, the pickers back
// where they were. Before, the preview failed with "Permission Denied" before it ran.
import { type IdleServer, startOwnServer } from "./harness.ts";
import { activeTab, editorTabs, expect, expectProblem, gridRows, runDone, signIn, test } from "./fixtures.ts";

let server: IdleServer;

test.beforeAll(async () => {
  server = await startOwnServer(["--enable-catalog-logging"]);
});
test.afterAll(async () => {
  await server?.stop();
});

const note = /_hardcharger_logs can't be a tab's catalog on this server: it is for querying by name/;

test("a preview of the log catalog's table runs, in the tab's own catalog, saying why", async ({ page }) => {
  // The server's refusal of a session in that catalog: the tab then opens one in its own.
  expectProblem(page, { status: 403, url: /\/chargewire\/v1\/sessions$/ });
  await signIn(page, server.admin, server.url);
  const tree = page.getByRole("tree", { name: "Servers" });
  const logs = tree.getByRole("treeitem", { name: "_hardcharger_logs", exact: true });
  await logs.getByRole("button", { name: "_hardcharger_logs", exact: true }).click();
  const main = logs.getByRole("treeitem", { name: "main", exact: true });
  await main.getByRole("button", { name: "main", exact: true }).click();
  const table = main.getByRole("treeitem", { name: "logs", exact: true }).locator(":scope > .tree-row-wrap");
  await table.hover();
  await table.getByRole("button", { name: /^More for/ }).click();
  await page.getByRole("menuitem", { name: "Preview data" }).click();
  const tab = activeTab(page);
  await expect(tab.getByTestId("query-error")).toHaveCount(0);
  await expect.poll(async () => (await gridRows(page)).length).toBeGreaterThan(1); // the server's own log lines
  await expect(tab.getByTestId("tab-notice")).toContainText(note);
  await expect(editorTabs(page).getByRole("tab", { selected: true })).toContainText("logs");
  // The tab stayed in its own catalog.
  await expect(tab.getByLabel("Catalog", { exact: true })).not.toHaveValue("_hardcharger_logs");
});

test("asking a tab to use the log catalog is refused with the note, the pickers back", async ({ page }) => {
  await signIn(page, server.admin, server.url);
  const tab = activeTab(page);
  const catalog = tab.getByLabel("Catalog", { exact: true });
  await expect(catalog).not.toHaveValue("");
  const before = await catalog.inputValue();
  await runDone(page, "SELECT 1"); // the tab's session is open: the change goes to it
  const tree = page.getByRole("tree", { name: "Servers" });
  const logs = tree.getByRole("treeitem", { name: "_hardcharger_logs", exact: true });
  await logs.getByRole("button", { name: "_hardcharger_logs", exact: true }).click();
  const main = logs.getByRole("treeitem", { name: "main", exact: true }).locator(":scope > .tree-row-wrap");
  await main.hover();
  await main.getByRole("button", { name: /^More for/ }).click();
  await page.getByRole("menuitem", { name: "Use this catalog and schema" }).click();
  await expect(tab.getByTestId("tab-notice")).toContainText(note);
  await expect(catalog).toHaveValue(before);
});
