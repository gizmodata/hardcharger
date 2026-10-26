import { expect, gridRows, runDone, signIn, test } from "./fixtures.ts";
import { startOwnServer } from "./harness.ts";

test("configuration edits the current editor session, respects overrides, and resets global to startup", async ({
  page,
}, testInfo) => {
  const server = await startOwnServer(["--query-timeout-seconds", "21"]);
  try {
    await signIn(page, server.admin, server.url);
    await runDone(page, "SELECT HARDCHARGER_CURRENT_SESSION() AS s");
    const id = (await gridRows(page))[1]![0]!;
    await page.getByRole("button", { name: "Server Configuration", exact: true }).click();
    await expect(page.getByTestId("settings-session-id")).toHaveText(id);
    await page.getByLabel("Filter server settings").fill("query_timeout");
    const row = page.locator('tr[data-setting="hardcharger.query_timeout"]');
    await expect(row.getByTestId("setting-session-value")).toContainText("21");
    await row.getByRole("button", { name: "Edit session hardcharger.query_timeout", exact: true }).click();
    await page.getByLabel("New setting value").fill("42");
    await page.getByRole("button", { name: "Apply change", exact: true }).click();
    await expect(row.getByTestId("setting-session-value")).toContainText("42");
    await expect(row.getByTestId("setting-session-value")).toContainText("Session override");
    await row.getByRole("button", { name: "Edit global hardcharger.query_timeout", exact: true }).click();
    await page.getByLabel("New setting value").fill("90");
    await page.getByRole("button", { name: "Apply change", exact: true }).click();
    await expect(row.getByTestId("setting-global-value")).toContainText("90");
    await expect(row.getByTestId("setting-session-value")).toContainText("42");
    await row.getByRole("button", { name: "Edit session hardcharger.query_timeout", exact: true }).click();
    await page.getByRole("button", { name: "Reset session override", exact: true }).click();
    await expect(row.getByTestId("setting-session-value")).toContainText("90");
    await expect(row.getByTestId("setting-session-value")).toContainText("Inherited");
    await row.getByRole("button", { name: "Edit global hardcharger.query_timeout", exact: true }).click();
    await page.getByRole("button", { name: "Reset to startup value", exact: true }).click();
    await expect(row.getByTestId("setting-global-value")).toContainText("21");
    await expect(row.getByTestId("setting-session-value")).toContainText("21");
    await page.screenshot({
      path: `../desktop/artifacts/sql-monitor/configuration-editable-${testInfo.project.name}.png`,
      fullPage: true,
    });
    await page.getByLabel("Filter server settings").fill("enable_instrumentation");
    const startup = page.locator('tr[data-setting="hardcharger.enable_instrumentation"]');
    await expect(startup).toContainText("restart required");
    await expect(startup.getByRole("button")).toHaveCount(0);
    await page.getByRole("button", { name: "Query", exact: true }).click();
    await runDone(page, "SELECT value FROM hardcharger_settings() WHERE name = 'hardcharger.query_timeout'");
    expect((await gridRows(page))[1]).toEqual(["21"]);
  } finally {
    await page.goto("about:blank");
    await server.stop();
  }
});

test("an administrator enables profiling for the current editor session from configuration", async ({ page }) => {
  const server = await startOwnServer(["--enable-instrumentation"]);
  try {
    await signIn(page, server.admin, server.url);
    await runDone(page, "SELECT 1");
    await page.getByRole("button", { name: "Server Configuration", exact: true }).click();
    await page.getByLabel("Filter server settings").fill("capture_query_profile");
    const row = page.locator('tr[data-setting="hardcharger.capture_query_profile"]');
    await row.getByRole("button", { name: "Edit session hardcharger.capture_query_profile", exact: true }).click();
    await page.getByLabel("New setting value").selectOption("detailed");
    await page.getByRole("button", { name: "Apply change", exact: true }).click();
    await expect(row.getByTestId("setting-session-value")).toContainText("detailed");
    await expect(row.getByTestId("setting-global-value")).toContainText("off");
    await page.getByRole("button", { name: "Query", exact: true }).click();
    await runDone(page, "SELECT sum(i) AS configured_profile_probe FROM range(10000) t(i)");
    await page.getByRole("button", { name: "SQL Monitor", exact: true }).click();
    await page.getByLabel("Query details", { exact: true }).selectOption("inline");
    await page
      .getByTestId("sql-monitor-table")
      .getByRole("button", { name: /SELECT sum\(i\) AS configured_profile_probe/ })
      .click();
    await expect(page.getByTestId("query-profile-plan")).toContainText("UNGROUPED_AGGREGATE");
  } finally {
    await page.goto("about:blank");
    await server.stop();
  }
});
