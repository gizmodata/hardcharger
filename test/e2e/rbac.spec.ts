import { expect, expectProblem, gridRows, runSql, signIn, test } from "./fixtures.ts";

test("a non-admin user sees only what access control allows", async ({ page, harness }) => {
  await signIn(page, harness.user);
  await expect(page.getByTestId("header-user")).toContainText("user");

  // The tenant's catalog only: not the other tenant's, not the server's own database.
  const tree = page.getByRole("tree", { name: "Catalogs" });
  const mine = tree.getByRole("treeitem", { name: harness.user.catalog, exact: true });
  await expect(mine).toHaveAttribute("aria-expanded", "true");
  await mine.getByRole("button", { name: "main", exact: true }).click();
  await expect(mine.getByRole("treeitem", { name: "orders", exact: true })).toBeVisible();
  await expect(tree.getByRole("treeitem", { name: harness.hiddenCatalog })).toHaveCount(0);
  await expect(tree.getByRole("treeitem", { name: harness.catalog, exact: true })).toHaveCount(0);

  await runSql(page, "SELECT count(*) AS n FROM orders");
  expect((await gridRows(page))[1]).toEqual(["5"]);

  // Another tenant's table reads exactly like one that does not exist.
  expectProblem(page, { status: 403, url: /\/statements$/ });
  await runSql(page, `SELECT * FROM ${harness.hiddenCatalog}.main.secrets`);
  await expect(page.getByTestId("query-error")).toContainText("permission denied");
  await expect(page.getByTestId("query-error")).toContainText("cannot see");
  await expect(page.getByTestId("query-error")).not.toContainText("beta-secret");

  // Sessions: only their own, and no way to end anyone's.
  await page.getByRole("button", { name: "Sessions" }).click();
  const table = page.getByTestId("sessions-table");
  await expect(table.locator("tbody tr").first()).toBeVisible();
  for (const user of await table.locator("tbody tr td:nth-child(2)").allTextContents()) {
    expect(user).toBe(harness.user.username);
  }
  await expect(table.getByRole("button", { name: /^End session/ })).toHaveCount(0);
});

test("an administrator ends a user's sessions; the user is sent to sign in again", async ({
  page,
  browser,
  harness,
}) => {
  await signIn(page, harness.user);
  await runSql(page, "SELECT 1 AS before");
  expect((await gridRows(page))[1]).toEqual(["1"]);

  const adminContext = await browser.newContext({ baseURL: harness.url });
  const admin = await adminContext.newPage();
  admin.on("dialog", (d) => void d.accept());
  await signIn(admin, harness.admin);
  await admin.getByRole("button", { name: "Sessions" }).click();
  const rows = admin.getByTestId("sessions-table").locator("tbody tr", { hasText: harness.user.username });
  await expect(rows.first()).toBeVisible();
  while ((await rows.count()) > 0) {
    const before = await rows.count();
    await rows
      .first()
      .getByRole("button", { name: /^End session/ })
      .click();
    await expect(rows).toHaveCount(before - 1);
  }
  await adminContext.close();

  expectProblem(page, { status: 401, url: /\/warp\/v1\/sessions\// });
  await runSql(page, "SELECT 2 AS after");
  await expect(page.getByTestId("signin-notice")).toHaveText("An administrator ended your session. Sign in again.");
  await expect(page.getByLabel("User name")).toHaveValue(harness.user.username);
});
