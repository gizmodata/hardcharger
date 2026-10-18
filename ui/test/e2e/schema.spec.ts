import { expect, gridRows, signIn, test } from "./fixtures.ts";

test("the schema browser lists catalogs, schemas, tables and columns; preview runs a query", async ({
  page,
  harness,
}) => {
  await signIn(page, harness.admin);
  const tree = page.getByRole("tree", { name: "Catalogs" });
  const catalog = tree.getByRole("treeitem", { name: harness.catalog, exact: true });
  await expect(catalog).toBeVisible();
  // The session's catalog starts open.
  await expect(catalog).toHaveAttribute("aria-expanded", "true");
  const sales = catalog.getByRole("treeitem", { name: "sales", exact: true });
  await sales.getByRole("button", { name: "sales" }).click();
  const orders = sales.getByRole("treeitem", { name: "orders", exact: true });
  await expect(orders).toBeVisible();
  await expect(sales.getByRole("treeitem", { name: "big_orders" })).toHaveAttribute("data-table-type", "VIEW");
  await orders.getByRole("button", { name: "orders", exact: true }).click();
  await expect(orders.getByRole("treeitem", { name: "amount" })).toContainText("DECIMAL");
  await expect(orders.getByRole("treeitem")).toHaveCount(4);

  await orders.getByRole("button", { name: "Preview orders" }).click();
  await expect(page.getByRole("tab", { selected: true })).toHaveText("orders");
  await expect(page.getByTestId("results-status")).toContainText("100 rows in");
  const rows = await gridRows(page);
  expect(rows[0]).toEqual(["id", "customer", "amount", "ordered_on"]);
  expect(rows[1]).toEqual(["0", "customer 0", "0.00", "2026-01-01"]);
  await page.getByRole("button", { name: "Close orders" }).click();
});

test("the filter narrows the tables shown", async ({ page, harness }) => {
  await signIn(page, harness.admin);
  const catalog = page.getByRole("treeitem", { name: harness.catalog, exact: true });
  const sales = catalog.getByRole("treeitem", { name: "sales", exact: true });
  await sales.getByRole("button", { name: "sales" }).click();
  await expect(sales.getByRole("treeitem", { name: "tricky" })).toBeVisible();
  await page.getByLabel("Filter tables").fill("big");
  await expect(sales.getByRole("treeitem", { name: "big_orders" })).toBeVisible();
  await expect(sales.getByRole("treeitem", { name: "tricky" })).toHaveCount(0);
});
