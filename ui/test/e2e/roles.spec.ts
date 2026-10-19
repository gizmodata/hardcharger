// Run as: a tab acts with one of the user's roles (SET ROLE), or all of them; the schema
// browser and autocomplete list what that role sees; the picker locks in a transaction; an
// administrator, who holds every right already, has no picker.
import type { Page } from "@playwright/test";

import { activeTab, editorFocused, expect, expectProblem, gridRows, runDone, signIn, test } from "./fixtures.ts";

const runAs = (page: Page) => activeTab(page).getByLabel("Run as");
const tree = (page: Page) => page.getByRole("tree", { name: "Catalogs" });

test("a user runs as one of their roles, or all of them", async ({ page, harness }) => {
  await signIn(page, harness.user);
  await editorFocused(page);
  const roles = harness.user.roles;
  await expect(runAs(page)).toHaveValue("");
  await expect(runAs(page).locator("option")).toHaveText(["All my roles", ...[...roles].sort()]);

  // All roles: both tables.
  await runDone(page, "SELECT count(*) FROM orders");
  expect((await gridRows(page))[1]).toEqual(["5"]);
  await runDone(page, "SELECT count(*) FROM invoices");
  expect((await gridRows(page))[1]).toEqual(["2"]);

  // As the readers role: orders only; the browser lists what that role sees.
  const readers = roles.find((r) => r.endsWith("/readers"));
  expect(readers).toBeDefined();
  await runAs(page).selectOption(readers ?? "");
  await runDone(page, "SELECT count(*) FROM orders");
  expect((await gridRows(page))[1]).toEqual(["5"]);
  expectProblem(page, { status: 403, url: /\/statements$/ });
  await runDone(page, "SELECT count(*) FROM invoices");
  await expect(activeTab(page).getByTestId("query-error")).toContainText("permission denied");
  const main = tree(page)
    .getByRole("treeitem", { name: harness.user.catalog, exact: true })
    .getByRole("treeitem", { name: "main", exact: true });
  if ((await main.getAttribute("aria-expanded")) !== "true")
    await main.getByRole("button", { name: "main", exact: true }).click();
  await expect(main.getByRole("treeitem", { name: "orders", exact: true })).toBeVisible();
  await expect(main.getByRole("treeitem", { name: "invoices", exact: true })).toHaveCount(0);

  // Locked while a transaction is open.
  await runDone(page, "BEGIN");
  await expect(runAs(page)).toBeDisabled();
  await runDone(page, "ROLLBACK");
  await expect(runAs(page)).toBeEnabled();

  // Back to all roles.
  await runAs(page).selectOption("");
  await runDone(page, "SELECT count(*) FROM invoices");
  expect((await gridRows(page))[1]).toEqual(["2"]);
  await expect(main.getByRole("treeitem", { name: "invoices", exact: true })).toBeVisible();
});

test("an administrator has no Run as picker", async ({ page, harness }) => {
  await signIn(page, harness.admin);
  await editorFocused(page);
  await runDone(page, "SELECT 1");
  await expect(runAs(page)).toHaveCount(0);
});
