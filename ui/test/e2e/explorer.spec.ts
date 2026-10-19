// The schema browser: a lazy tree of catalogs, schemas, Tables and Views, and columns; names
// inserted fully qualified and quoted by double-click or drag; its menu; search; re-listing
// after a change; and the side panel hidden or resized.
import type { Page } from "@playwright/test";

import {
  activeTab,
  editorFocused,
  editorText,
  expect,
  gridRows,
  pasted,
  runDone,
  setSql,
  signIn,
  test,
  editorTabs,
} from "./fixtures.ts";

test.beforeEach(async ({ page, harness }) => {
  await signIn(page, harness.admin);
  await editorFocused(page);
});

function tree(page: Page) {
  return page.getByRole("tree", { name: "Catalogs" });
}
async function openSales(page: Page, catalog: string) {
  const sales = tree(page)
    .getByRole("treeitem", { name: catalog, exact: true })
    .getByRole("treeitem", { name: "sales", exact: true });
  if ((await sales.getAttribute("aria-expanded")) !== "true")
    await sales.getByRole("button", { name: "sales", exact: true }).click();
  await expect(sales.getByRole("treeitem", { name: "Tables" })).toHaveAttribute("aria-expanded", "true");
  return sales;
}
async function menu(page: Page, item: import("@playwright/test").Locator, label: string) {
  const row = item.locator(":scope > .tree-row-wrap");
  await row.hover(); // the … button shows on hover (and on focus)
  await row.getByRole("button", { name: /^More for/ }).click();
  await page.getByRole("menuitem", { name: label }).click();
}

test("catalogs, schemas, Tables and Views, columns with their types, each listed when opened", async ({
  page,
  harness,
}) => {
  const catalog = tree(page).getByRole("treeitem", { name: harness.catalog, exact: true });
  // The active tab's catalog starts open; `temp` (a session's own) is left out.
  await expect(catalog).toHaveAttribute("aria-expanded", "true");
  await expect(tree(page).getByRole("treeitem", { name: "temp", exact: true })).toHaveCount(0);
  const sales = await openSales(page, harness.catalog);
  await expect(sales.getByRole("treeitem", { name: "Tables" })).toContainText(/Tables \(\d+\)/);
  await expect(sales.getByRole("treeitem", { name: "Views" })).toContainText("Views (1)");
  const orders = sales.getByRole("treeitem", { name: "orders", exact: true });
  await expect(sales.getByRole("treeitem", { name: "big_orders" })).toHaveAttribute("data-table-type", "VIEW");
  await orders.getByRole("button", { name: "orders", exact: true }).click();
  await expect(orders.getByRole("treeitem", { name: "amount" })).toContainText("DECIMAL");
  await expect(orders.getByRole("treeitem")).toHaveCount(4);
});

test("double-click inserts a name, fully qualified and quoted where needed; drag drops it", async ({
  page,
  harness,
}) => {
  const sales = await openSales(page, harness.catalog);
  // Created while sales is open: the CREATE re-lists what is open.
  await runDone(page, 'CREATE OR REPLACE TABLE sales."Mixed Case" (id INTEGER, "Odd Col" VARCHAR)');
  const mixed = sales.getByRole("treeitem", { name: "Mixed Case", exact: true });
  await expect(mixed).toBeVisible();
  await setSql(page, "SELECT * FROM ");
  await mixed.getByRole("button", { name: "Mixed Case", exact: true }).dblclick();
  await expect.poll(() => editorText(page)).toBe(`SELECT * FROM ${harness.catalog}.sales."Mixed Case"`);
  // Its columns: just the name, quoted.
  await mixed.getByRole("button", { name: "Mixed Case", exact: true }).click();
  await page.keyboard.press("End");
  await setSql(page, "SELECT ");
  await mixed.getByRole("button", { name: "Odd Col" }).dblclick();
  await expect.poll(() => editorText(page)).toBe('SELECT "Odd Col"');

  // Dragged into the editor, it goes where it is dropped.
  await setSql(page, "SELECT * FROM ");
  const orders = sales.getByRole("treeitem", { name: "orders", exact: true });
  await orders.getByRole("button", { name: "orders", exact: true }).dragTo(activeTab(page).locator(".view-lines"), {
    targetPosition: { x: 400, y: 8 },
  });
  await expect.poll(() => editorText(page)).toBe(`SELECT * FROM ${harness.catalog}.sales.orders`);
  await runDone(page, 'DROP TABLE sales."Mixed Case"');
});

test("the menu: preview, SELECT, column names, copy, a view's definition, use this catalog and schema", async ({
  page,
  harness,
}) => {
  const sales = await openSales(page, harness.catalog);
  const orders = sales.getByRole("treeitem", { name: "orders", exact: true });

  await menu(page, orders, "Preview data");
  await expect(editorTabs(page).getByRole("tab", { selected: true })).toHaveText("orders");
  await expect(activeTab(page).getByTestId("results-status")).toContainText("100 rows in");
  await expect(activeTab(page).getByLabel("Schema")).toHaveValue("sales");

  await setSql(page, "");
  await menu(page, orders, "Insert SELECT statement");
  await expect.poll(() => editorText(page)).toBe(`SELECT * FROM ${harness.catalog}.sales.orders LIMIT 100;`);
  await setSql(page, "");
  await menu(page, orders, "Insert column names");
  await expect.poll(() => editorText(page)).toBe("id, customer, amount, ordered_on");
  await menu(page, orders, "Copy name");
  expect(await pasted(page)).toBe(`${harness.catalog}.sales.orders`);

  const view = sales.getByRole("treeitem", { name: "big_orders", exact: true });
  await menu(page, view, "Show definition");
  const definition = page.getByTestId("view-definition");
  await expect(definition).toContainText("CREATE VIEW");
  await expect(definition).toContainText("amount > 1000");
  await page.getByRole("button", { name: "Open in a new tab" }).click();
  await expect.poll(() => editorText(page)).toContain("CREATE VIEW");

  // Right-click opens the same menu. Use this catalog and schema points the active tab there.
  await activeTab(page).getByLabel("Schema").selectOption("main");
  await sales.locator(":scope > .tree-row-wrap").click({ button: "right" });
  await page.getByRole("menuitem", { name: "Use this catalog and schema" }).click();
  await expect(activeTab(page).getByLabel("Schema")).toHaveValue("sales");
  await runDone(page, "SELECT current_schema() AS s");
  expect((await gridRows(page))[1]).toEqual(["sales"]);
  // Not while a transaction is open.
  await runDone(page, "BEGIN");
  await sales.locator(":scope > .tree-row-wrap").click({ button: "right" });
  await expect(page.getByRole("menuitem", { name: "Use this catalog and schema" })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toHaveCount(0);
  await runDone(page, "ROLLBACK");
});

test("search finds tables and views anywhere; a hit shows in the tree", async ({ page, harness }) => {
  const search = page.getByLabel("Search tables and views");
  await search.fill("o");
  await expect(page.getByTestId("search-results")).toHaveCount(0); // two characters at least
  await search.fill("ORDER");
  const results = page.getByTestId("search-results");
  await expect(results.getByRole("button", { name: /big_orders/ })).toBeVisible();
  await expect(results.getByRole("button", { name: /^orders/ }).first()).toBeVisible();
  // The other tenant's catalog is listed for an administrator.
  await results.getByRole("button", { name: /big_orders/ }).click();
  await expect(search).toHaveValue("");
  const view = tree(page)
    .getByRole("treeitem", { name: harness.catalog, exact: true })
    .getByRole("treeitem", { name: "big_orders", exact: true });
  await expect(view).toBeVisible();
  await expect(view.locator(":scope > .tree-row-wrap")).toHaveClass(/tree-found/);
  await search.fill("no_such_thing");
  await expect(page.getByTestId("search-results")).toContainText("No tables or views match.");
  await search.press("Escape");
  await expect(search).toHaveValue("");
});

test("the side panel hides and shows, and resizes (kept in this browser)", async ({ page }) => {
  const panel = page.getByTestId("side-panel");
  const width = async () => (await panel.boundingBox())!.width;
  const before = await width();
  await page.getByTestId("side-panel-resize").focus();
  await page.keyboard.press("Shift+ArrowRight");
  await expect.poll(width).toBe(before + 80);
  await page.getByRole("button", { name: "Hide the side panel" }).click();
  await expect(panel).toHaveCount(0);
  await page.reload();
  await expect(page.getByTestId("header-user")).toBeVisible();
  await expect(panel).toHaveCount(0);
  await page.getByRole("button", { name: "Show the side panel" }).click();
  await expect.poll(width).toBe(before + 80);
  await page.getByTestId("side-panel-resize").dblclick();
  await expect.poll(width).toBe(before);
});
