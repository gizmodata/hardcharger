// System Admin (Hardcharger 2.14): a user granted SYSTEM ADMIN acts as the administrator in a
// session that says SET ROLE SYSTEM ADMIN. The UI follows what the tab's session answers: the
// admin pages are offered only while a tab acts as one, and they act as one through sessions
// of their own; RESET ROLE and a revoke take them away again, each found out from the server.
import { type Connection, connect } from "@gizmodata/chargewire";
import { randomBytes } from "node:crypto";
import type { Page } from "@playwright/test";

import { activeTab, expect, expectProblem, gridRows, runDone, signIn, test } from "./fixtures.ts";
import type { HarnessState } from "./harness.ts";

const nav = (page: Page, name: string) => page.getByRole("button", { name, exact: true });
const tabBadge = (page: Page) => activeTab(page).getByTestId("tab-system-admin");
const headerChip = (page: Page) => page.getByTestId("header-system-admin");

/** A system-scope user holding SYSTEM ADMIN (tenant users can't), made by the administrator. */
async function systemAdminUser(harness: HarnessState): Promise<{
  who: { username: string; password: string };
  admin: Connection;
}> {
  const username = `sysadmin_${randomBytes(4).toString("hex")}`;
  const password = `sa:${randomBytes(6).toString("hex")}@e2e`;
  const admin = await connect({ url: harness.serverUrl, ...harness.admin });
  await admin.execute(`CREATE USER ${username} WITH PASSWORD '${password}'`);
  await admin.execute(`GRANT SYSTEM ADMIN TO ${username}`);
  // Signing in reads what the server last polled from the store (about once a second).
  await expect
    .poll(
      async () => {
        try {
          const own = await connect({ url: harness.serverUrl, username, password });
          await own.close();
          return true;
        } catch {
          return false;
        }
      },
      { timeout: 15_000 },
    )
    .toBe(true);
  return { who: { username, password }, admin };
}

test("the admin pages are offered while a tab acts as System Admin, and only then", async ({ page, harness }) => {
  const { who, admin } = await systemAdminUser(harness);
  try {
    await signIn(page, who);
    // Holding it changes nothing by itself.
    await expect(page.getByTestId("header-user")).toContainText("user");
    await expect(nav(page, "Server Configuration")).toHaveCount(0);
    await expect(nav(page, "SQL Monitor")).toHaveCount(0);
    await expect(headerChip(page)).toHaveCount(0);
    await nav(page, "Sessions").click();
    const sessions = page.getByTestId("sessions-table");
    await expect(sessions.locator("tbody tr").first()).toBeVisible();
    for (const user of await sessions.locator("tbody tr td:nth-child(2)").allTextContents())
      expect(user).toBe(who.username);
    await expect(sessions.getByRole("button", { name: /^End session/ })).toHaveCount(0);

    // SET ROLE SYSTEM ADMIN in a tab: the tab, the header and the navigation say so.
    await nav(page, "Query").click();
    await runDone(page, "SET ROLE SYSTEM ADMIN");
    await expect(tabBadge(page)).toBeVisible();
    await expect(headerChip(page)).toBeVisible();
    await expect(nav(page, "Server Configuration")).toBeVisible();
    // This server runs without instrumentation: offered, and disabled as for the administrator.
    await expect(nav(page, "SQL Monitor")).toBeDisabled();

    // Server Configuration, through a session of its own that acts as System Admin.
    await nav(page, "Server Configuration").click();
    await expect(page.getByTestId("system-admin-notice")).toBeVisible();
    await expect(page.getByTestId("server-configuration-table")).toContainText("hardcharger.max_concurrent_statements");
    // Sessions: everyone's, with End, as the administrator sees them.
    await nav(page, "Sessions").click();
    await expect(page.getByTestId("system-admin-notice")).toBeVisible();
    const adminRow = sessions.locator("tbody tr", { hasText: admin.sessionId });
    await expect(adminRow).toHaveCount(1);
    await expect(adminRow.getByRole("button", { name: /^End session/ })).toBeVisible();

    // RESET ROLE: gone, from the tab's own answer.
    await nav(page, "Query").click();
    await runDone(page, "RESET ROLE");
    await expect(tabBadge(page)).toHaveCount(0);
    await expect(headerChip(page)).toHaveCount(0);
    await expect(nav(page, "Server Configuration")).toHaveCount(0);
    await nav(page, "Sessions").click();
    await expect(sessions.locator("tbody tr", { hasText: admin.sessionId })).toHaveCount(0);
    await expect(sessions.getByRole("button", { name: /^End session/ })).toHaveCount(0);
    await page.goto(`${harness.url}/ui/configuration`);
    await expect(page.getByText("Only unrestricted administrators can view server configuration.")).toBeVisible();
    await expect(page.getByTestId("system-admin-hint")).toBeVisible();
  } finally {
    await admin.execute(`DROP USER ${who.username}`).catch(() => {});
    await admin.close();
  }
});

test("a revoke closes the admin pages and the tab's System Admin, as the server refuses them", async ({
  page,
  harness,
}) => {
  const { who, admin } = await systemAdminUser(harness);
  try {
    await signIn(page, who);
    await runDone(page, "SET ROLE SYSTEM ADMIN");
    await expect(tabBadge(page)).toBeVisible();
    await nav(page, "Server Configuration").click();
    const table = page.getByTestId("server-configuration-table");
    await expect(table).toContainText("hardcharger.max_concurrent_statements");

    // Revoked by the administrator while the page is open. Nothing in the UI changes by itself;
    // the page's next request is refused by the server, and then every tab asks its session.
    await admin.execute(`REVOKE SYSTEM ADMIN FROM ${who.username}`);
    expectProblem(page, { status: 403, url: /\/statements$/ });
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(page.getByText("Only unrestricted administrators can view server configuration.")).toBeVisible();
    await expect(nav(page, "Server Configuration")).toHaveCount(0);
    await expect(headerChip(page)).toHaveCount(0);
    await nav(page, "Query").click();
    await expect(tabBadge(page)).toHaveCount(0);

    // The tab's session runs nothing until RESET ROLE, and says why.
    await runDone(page, "SELECT 1 AS refused");
    await expect(activeTab(page).getByTestId("query-error")).toContainText("no longer holds SYSTEM ADMIN");
    await runDone(page, "RESET ROLE");
    await runDone(page, "SELECT 2 AS mine");
    expect((await gridRows(page))[1]).toEqual(["2"]);
    await expect(tabBadge(page)).toHaveCount(0);

    // Granted again: it takes a SET ROLE in a tab, as before.
    await admin.execute(`GRANT SYSTEM ADMIN TO ${who.username}`);
    await expect(nav(page, "Server Configuration")).toHaveCount(0);
    await runDone(page, "SET ROLE SYSTEM ADMIN");
    await expect(nav(page, "Server Configuration")).toBeVisible();
  } finally {
    await admin.execute(`DROP USER ${who.username}`).catch(() => {});
    await admin.close();
  }
});

test("a tab revoked while idle finds out at its next statement", async ({ page, harness }) => {
  const { who, admin } = await systemAdminUser(harness);
  try {
    await signIn(page, who);
    await runDone(page, "SET ROLE SYSTEM ADMIN");
    await expect(headerChip(page)).toBeVisible();
    await admin.execute(`REVOKE SYSTEM ADMIN FROM ${who.username}`);
    expectProblem(page, { status: 403, url: /\/statements$/ });
    await runDone(page, "SELECT 1 AS refused");
    await expect(activeTab(page).getByTestId("query-error")).toContainText("no longer holds SYSTEM ADMIN");
    await expect(tabBadge(page)).toHaveCount(0);
    await expect(headerChip(page)).toHaveCount(0);
    await expect(nav(page, "Server Configuration")).toHaveCount(0);
  } finally {
    await admin.execute(`DROP USER ${who.username}`).catch(() => {});
    await admin.close();
  }
});

test("ending the session of the only tab that acts as System Admin closes the admin pages", async ({
  page,
  harness,
}) => {
  const { who, admin } = await systemAdminUser(harness);
  try {
    await signIn(page, who);
    await runDone(page, "SET ROLE SYSTEM ADMIN");
    await expect(nav(page, "Server Configuration")).toBeVisible();
    // New session ends the tab's session (and so what it acted as).
    await activeTab(page)
      .getByRole("button", { name: /New session/ })
      .click();
    await expect(tabBadge(page)).toHaveCount(0);
    await expect(nav(page, "Server Configuration")).toHaveCount(0);
    // A new session is the user's: nothing carries over.
    await runDone(page, "SELECT HARDCHARGER_IS_SYSTEM_ADMIN() AS acting");
    expect((await gridRows(page))[1]).toEqual(["false"]);
    await expect(nav(page, "Server Configuration")).toHaveCount(0);
  } finally {
    await admin.execute(`DROP USER ${who.username}`).catch(() => {});
    await admin.close();
  }
});
