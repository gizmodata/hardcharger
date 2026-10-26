import { connect } from "@gizmodata/warp-sql";
import { mkdirSync } from "node:fs";
import path from "node:path";

import { expect, runDone, signIn, test } from "./fixtures.ts";
import { startOwnServer } from "./harness.ts";

test("SQL Monitor is disabled without instrumentation and configuration remains available", async ({
  page,
  harness,
}) => {
  await signIn(page, harness.admin);
  await expect(page.getByRole("button", { name: "SQL Monitor", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Server Configuration", exact: true }).click();
  await expect(page.getByTestId("server-configuration-table")).toContainText("hardcharger.max_concurrent_statements");
  await expect(page.getByTestId("server-configuration-table")).not.toContainText(harness.admin.password);
  await page.getByLabel("Filter server settings").fill("enable_instrumentation");
  await expect(page.getByTestId("server-configuration-table").locator("tbody tr")).toHaveCount(1);
});

test("a non-admin cannot see admin tabs or enter their routes", async ({ page, harness }) => {
  await signIn(page, harness.user);
  await expect(page.getByRole("button", { name: "SQL Monitor", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Server Configuration", exact: true })).toHaveCount(0);
  await page.goto(`${harness.url}/ui/monitor`);
  await expect(page.getByText("Only unrestricted administrators can view SQL Monitor.")).toBeVisible();
});

test("SQL Monitor shows outcomes and slots and preserves the editor", async ({ page }, testInfo) => {
  const server = await startOwnServer(["--enable-instrumentation", "--max-concurrent-statements", "2"]);
  const connection = await connect({ url: `${server.url}/warp`, ...server.admin, resultPartitions: 1 });
  try {
    await connection.query("SELECT 42 AS monitor_completed");
    try {
      await connection.query("SELECT * FROM missing_monitor_demo_table");
    } catch {
      /* expected SQL error */
    }
    await signIn(page, server.admin, server.url);
    await runDone(page, "SELECT 7 AS preserved_editor_result");
    await page.getByRole("button", { name: "SQL Monitor", exact: true }).click();
    await expect(page.getByRole("heading", { name: "SQL Monitor" })).toBeVisible();
    const table = page.getByTestId("sql-monitor-table");
    await expect(table).toContainText("monitor_completed");
    await expect(table).toContainText("missing_monitor_demo_table");
    await expect(table).toContainText("Succeeded");
    await expect(table).toContainText("Failed");
    await expect(page.locator(".monitor-page").getByRole("alert")).toHaveCount(0);
    await expect(page.getByLabel("Recent history")).toHaveValue("10");
    await page.getByLabel("Filter monitored queries").fill("monitor_completed");
    await expect(table.locator("tbody tr")).toHaveCount(1);
    await table.getByRole("button", { name: /monitor_completed/ }).click();
    await expect(page.getByRole("region", { name: "Statement details" })).toContainText("Succeeded");
    await page.getByLabel("Filter monitored queries").clear();
    const artifactDir = path.resolve("../desktop/artifacts/sql-monitor");
    mkdirSync(artifactDir, { recursive: true });
    await page.screenshot({ path: path.join(artifactDir, `sql-monitor-${testInfo.project.name}.png`), fullPage: true });
    await page.getByRole("button", { name: "Server Configuration", exact: true }).click();
    await expect(page.getByTestId("server-configuration-table")).toContainText("hardcharger.enable_instrumentation");
    await page.screenshot({
      path: path.join(artifactDir, `server-configuration-${testInfo.project.name}.png`),
      fullPage: true,
    });
    await page.getByRole("button", { name: "Query", exact: true }).click();
    await expect(page.getByRole("grid")).toContainText("preserved_editor_result");
  } finally {
    await connection.close();
    await server.stop();
  }
});
