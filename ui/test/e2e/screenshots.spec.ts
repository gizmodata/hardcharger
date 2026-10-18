// Screenshots of the main views, light and dark, kept as test artifacts (CI uploads them).
// The run fails on any console error or failed request along the way, as every test does.
import { expect, gridRows, runSql, signIn, test } from "./fixtures.ts";

test("screenshots of the main views", async ({ page, harness }, testInfo) => {
  const shot = (name: string) => page.screenshot({ path: testInfo.outputPath(`${name}.png`) });
  await page.goto("/ui/");
  await expect(page.getByTestId("server-version")).toContainText("Hardcharger");
  await shot("sign-in");

  await signIn(page, harness.admin);
  const catalog = page.getByRole("treeitem", { name: harness.catalog, exact: true });
  const sales = catalog.getByRole("treeitem", { name: "sales", exact: true });
  await sales.getByRole("button", { name: "sales" }).click();
  const orders = sales.getByRole("treeitem", { name: "orders", exact: true });
  await orders.getByRole("button", { name: "orders", exact: true }).click();
  await expect(orders.getByRole("treeitem", { name: "amount" })).toBeVisible();
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
  await shot("query-light");

  await page.getByRole("button", { name: "Dark theme" }).click();
  await shot("query-dark");

  await page.getByRole("button", { name: "Sessions" }).click();
  await expect(page.getByTestId("sessions-table")).toBeVisible();
  await shot("sessions-dark");
  await page.getByRole("button", { name: "Light theme" }).click();
});
