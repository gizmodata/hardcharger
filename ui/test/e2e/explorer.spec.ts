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

// A narrow panel shortens a column's type before its name: `l_orderkey  BIG…`, never
// `l_or…  BIGINT NOT NULL`; a name is shortened only once its type is down to a few letters.
// The row's tooltip has both whole, and nothing (the … button, the scroll bar) covers them.
test("a narrow panel shortens the column type first, then the name", async ({ page, harness }) => {
  const longName = "l_comment_that_goes_on_and_on_and_on_forever";
  await runDone(
    page,
    `CREATE OR REPLACE TABLE sales.rc_narrow (l_orderkey BIGINT NOT NULL, ` +
      `l_shipdate TIMESTAMP WITH TIME ZONE, ${longName} VARCHAR)`,
  );
  // A narrow panel (268 px; the narrowest is 220), short enough that the tree scrolls.
  await page.setViewportSize({ width: 1360, height: 560 });
  const handle = page.getByTestId("side-panel-resize");
  await handle.focus();
  await page.keyboard.press("Home");
  for (let i = 0; i < 3; i++) await page.keyboard.press("ArrowRight");
  await expect.poll(async () => (await page.getByTestId("side-panel").boundingBox())?.width).toBe(268);

  const sales = await openSales(page, harness.catalog);
  const table = sales.getByRole("treeitem", { name: "rc_narrow", exact: true });
  await table.getByRole("button", { name: "rc_narrow", exact: true }).click();
  await expect(table.getByRole("treeitem")).toHaveCount(3);

  const measure = (name: string) =>
    table
      .getByRole("treeitem", { name, exact: true })
      .locator(":scope > .tree-row-wrap")
      .evaluate((wrap) => {
        const part = (sel: string) => {
          const el = wrap.querySelector<HTMLElement>(sel)!;
          const r = el.getBoundingClientRect();
          return { cut: el.scrollWidth > el.clientWidth + 1, left: r.left, right: r.right, width: r.width };
        };
        const tree = wrap.closest<HTMLElement>(".side-tree")!;
        const t = tree.getBoundingClientRect();
        return {
          name: part(".tree-name"),
          type: part(".tree-type"),
          action: part(".tree-action"),
          // Where the tree's content ends: its scroll bar, if any, is to the right of this.
          contentRight: t.left + tree.clientLeft + tree.clientWidth,
          scrolls: tree.scrollHeight > tree.clientHeight,
          gutter: tree.offsetWidth - tree.clientWidth,
          title: wrap.querySelector(".tree-row")!.getAttribute("title") ?? "",
        };
      });

  // A short name stays whole; its long type is shortened.
  const ship = await measure("l_shipdate");
  expect(ship.scrolls, "the tree scrolls").toBe(true);
  expect(ship.name.cut, "l_shipdate is shown whole").toBe(false);
  expect(ship.type.cut, "its type is shortened").toBe(true);
  expect(ship.type.width).toBeGreaterThan(10);
  expect(ship.title).toContain("l_shipdate");
  expect(ship.title).toContain("TIMESTAMP WITH TIME ZONE");
  const key = await measure("l_orderkey");
  expect(key.name.cut, "l_orderkey is shown whole").toBe(false);
  expect(key.title).toContain("BIGINT NOT NULL");
  // A name too long for the row is shortened too, once its type is down to a few letters.
  const long = await measure(longName);
  expect(long.name.cut).toBe(true);
  expect(long.type.width).toBeLessThan(40);
  expect(long.title).toContain(longName);
  // Nothing overlaps: name, type, then the … button, all left of the scroll bar (with room
  // for one drawn over the content, as macOS does, when it takes no width of its own).
  for (const m of [ship, key, long]) {
    expect(m.name.right).toBeLessThanOrEqual(m.type.left + 0.5);
    expect(m.type.right).toBeLessThanOrEqual(m.action.left + 0.5);
    expect(m.action.right).toBeLessThanOrEqual(m.contentRight - (m.gutter > 0 ? 0 : 7) + 0.5);
  }
  await runDone(page, "DROP TABLE sales.rc_narrow");
});
