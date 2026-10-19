// Screenshots of the main views, light and dark, kept as test artifacts (CI uploads them).
// The run fails on any console error or failed request along the way, as every test does.
import {
  activeTab,
  editorFocused,
  expect,
  gridRows,
  runDone,
  runSql,
  setSql,
  signIn,
  test,
  editorTabs,
} from "./fixtures.ts";

test("screenshots of the main views", async ({ page, harness }, testInfo) => {
  const shot = (name: string) => page.screenshot({ path: testInfo.outputPath(`${name}.png`) });
  await page.goto("/ui/");
  await expect(page.getByTestId("server-version")).toContainText("Hardcharger");
  await shot("sign-in-light");
  await page.getByRole("button", { name: "Dark theme" }).click();
  await shot("sign-in-dark");
  await page.getByRole("button", { name: "Light theme" }).click();

  await signIn(page, harness.admin);
  await editorFocused(page);
  const catalog = page.getByRole("treeitem", { name: harness.catalog, exact: true });
  const sales = catalog.getByRole("treeitem", { name: "sales", exact: true });
  await sales.getByRole("button", { name: "sales" }).click();
  const orders = sales.getByRole("treeitem", { name: "orders", exact: true });
  await orders.getByRole("button", { name: "orders", exact: true }).click();
  await expect(orders.getByRole("treeitem", { name: "amount" })).toBeVisible();
  // A second tab with a script (a result tab per result set) and an open transaction, then
  // back to the first tab, named, with its result.
  await editorTabs(page).getByRole("tab", { name: "Query 1" }).dblclick();
  await page.getByLabel("Tab name").fill("Top customers");
  await page.getByLabel("Tab name").press("Enter");
  await page.getByRole("button", { name: "New tab" }).click();
  await runDone(page, "BEGIN");
  await setSql(
    page,
    "SELECT count(*) AS orders FROM sales.orders;\nSELECT customer, max(amount) AS largest FROM sales.orders GROUP BY ALL ORDER BY 1;",
  );
  await page.keyboard.press("ControlOrMeta+Shift+Enter");
  await expect(activeTab(page).getByRole("tab", { name: "Messages" })).toBeVisible();
  await shot("script-light");
  await page.getByRole("button", { name: "Dark theme" }).click();
  await shot("script-dark");
  await page.getByRole("button", { name: "Light theme" }).click();
  await activeTab(page).getByRole("button", { name: "Rollback" }).click();
  await expect(activeTab(page).getByTestId("in-transaction")).toHaveCount(0);
  await editorTabs(page).getByRole("tab", { name: "Top customers" }).click();
  await runSql(
    page,
    "SELECT customer, count(*) AS orders, sum(amount) AS total, min(ordered_on) AS first_order\n" +
      "FROM sales.orders\nGROUP BY customer\nORDER BY total DESC;",
  );
  await expect.poll(async () => (await gridRows(page)).length).toBe(8);
  await page.getByTestId("results-grid").getByRole("gridcell").nth(1).click();
  await page
    .getByTestId("results-grid")
    .getByRole("gridcell")
    .nth(10)
    .click({ modifiers: ["Shift"] });
  // The selection stays; the grid's focus ring doesn't belong in a picture.
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await shot("query-light");

  await page.getByRole("button", { name: "Dark theme" }).click();
  await shot("query-dark");

  // The schema browser: a search, and a view's menu.
  await page.getByLabel("Search tables and views").fill("order");
  await expect(page.getByTestId("search-results").getByRole("button").first()).toBeVisible();
  const view = sales.getByRole("treeitem", { name: "big_orders", exact: true });
  await view.locator(":scope > .tree-row-wrap").click({ button: "right" });
  await expect(page.getByRole("menuitem", { name: "Show definition" })).toBeVisible();
  await shot("explorer-dark");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Light theme" }).click();
  await view.locator(":scope > .tree-row-wrap").click({ button: "right" });
  await shot("explorer-light");
  await page.keyboard.press("Escape");
  await page.getByLabel("Search tables and views").fill("");
  await page.getByRole("button", { name: "Dark theme" }).click();

  // Saved queries: a folder with the tab's query in it, and the results' Download menu.
  await page.getByRole("tab", { name: "Saved queries" }).click();
  await page.getByTestId("saved-queries").getByRole("button", { name: "New folder" }).click();
  await page.getByLabel("New name").fill("Reports");
  await page.getByLabel("New name").press("Enter");
  await page.getByRole("tree", { name: "Saved queries" }).getByRole("button", { name: "Reports" }).click();
  await activeTab(page).getByTestId("save-button").click();
  await page.getByTestId("save-query-dialog").getByRole("button", { name: "Save", exact: true }).click();
  await expect(
    page.getByRole("tree", { name: "Saved queries" }).getByRole("treeitem", { name: "Top customers" }),
  ).toBeVisible();
  await activeTab(page).getByTestId("export-button").click();
  await shot("saved-dark");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Light theme" }).click();
  await activeTab(page).getByTestId("export-button").click();
  await shot("saved-light");
  await page.keyboard.press("Escape");
  await page.getByRole("tab", { name: "Data" }).click();
  await page.getByRole("button", { name: "Dark theme" }).click();

  await page.getByRole("button", { name: "Sessions" }).click();
  await expect(page.getByTestId("sessions-table")).toBeVisible();
  await shot("sessions-dark");
  await page.getByRole("button", { name: "Light theme" }).click();
});
