import { connect } from "@gizmodata/chargewire";
import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";

import { expect, expectProblem, runDone, runSql, signIn, test } from "./fixtures.ts";
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
  await page.goto(`${harness.url}/ui/monitor/query?execution=q_test`);
  await expect(page.getByText("Only unrestricted administrators can view query details.")).toBeVisible();
  await expect(page.getByRole("region", { name: "Statement details" })).toHaveCount(0);
});

test("navigation explicitly aborts an outstanding metadata request before replacing the page", async ({
  page,
  harness,
}) => {
  await page.addInitScript(() => {
    const original = globalThis.fetch.bind(globalThis);
    globalThis.fetch = (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (url.includes("/metadata/db-schemas")) {
        init?.signal?.addEventListener("abort", () => sessionStorage.setItem("metadata-aborted-on-exit", "yes"), {
          once: true,
        });
      }
      return original(input, init);
    };
  });
  let requested!: () => void;
  const started = new Promise<void>((resolve) => {
    requested = resolve;
  });
  let resume!: () => void;
  const held = new Promise<void>((resolve) => {
    resume = resolve;
  });
  let first = true;
  await page.route("**/metadata/db-schemas?*", async (route) => {
    if (!first) return route.continue();
    first = false;
    requested();
    await held;
    await route.abort().catch(() => {});
  });
  try {
    await signIn(page, harness.user);
    await started;
    await page.goto(`${harness.url}/ui/monitor`);
    await expect(page.getByText("Only unrestricted administrators can view SQL Monitor.")).toBeVisible();
    expect(await page.evaluate(() => sessionStorage.getItem("metadata-aborted-on-exit"))).toBe("yes");
  } finally {
    resume();
  }
});

test("SQL Monitor shows outcomes and slots and preserves the editor", async ({ page }, testInfo) => {
  const server = await startOwnServer(["--enable-instrumentation", "--max-concurrent-statements", "2"]);
  const connection = await connect({ url: `${server.url}/chargewire`, ...server.admin, resultPartitions: 1 });
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
    await page.getByLabel("Query details", { exact: true }).selectOption("inline");
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
    await page.goto("about:blank");
    await server.stop();
  }
});

test("statement details render captured profile metrics without rerunning the query", async ({ page }, testInfo) => {
  const server = await startOwnServer(["--enable-instrumentation"]);
  const connection = await connect({ url: `${server.url}/chargewire`, ...server.admin, resultPartitions: 1 });
  const sql = "SELECT sum(i) AS captured_profile_ui_probe FROM range(100000) t(i) WHERE i % 2 = 0";
  try {
    await connection.execute("SET hardcharger.capture_query_profile = 'detailed'");
    await connection.query(sql);
    await connection.execute("SET hardcharger.capture_query_profile = 'off'");
    await connection.query("SELECT 42 AS no_profile_ui_probe");
    await signIn(page, server.admin, server.url);
    await page.getByRole("button", { name: "SQL Monitor", exact: true }).click();
    const table = page.getByTestId("sql-monitor-table");
    await page.getByLabel("Query details", { exact: true }).selectOption("inline");
    await table.getByRole("button", { name: /captured_profile_ui_probe/ }).click();
    const profile = page.getByRole("region", { name: "Captured query profile" });
    await expect(profile.getByTestId("query-profile-plan")).toContainText("UNGROUPED_AGGREGATE");
    await expect(profile).toContainText("query.total_time");
    await expect(profile).toContainText("system.peak_buffer_memory");
    await expect(profile).toContainText("io.total_bytes_read");
    await expect(profile).toContainText("optimizer");
    await expect(profile).toContainText("Current global capture: off");
    const download = page.waitForEvent("download");
    await profile.getByRole("button", { name: "Download profile JSON" }).click();
    const raw = readFileSync(await (await download).path(), "utf8");
    expect((JSON.parse(raw) as { query: { sql: string } }).query.sql).toBe(sql);
    const counted = await connection.query(
      `SELECT count(*) AS n FROM _hardcharger_instr.execution_details WHERE sql_text = '${sql}'`,
    );
    expect(counted.getChildAt(0)!.get(0)).toBe(1n);
    const artifactDir = path.resolve("../desktop/artifacts/sql-monitor");
    mkdirSync(artifactDir, { recursive: true });
    await page.getByRole("button", { name: "Pause", exact: true }).click();
    await profile.scrollIntoViewIfNeeded();
    await page.screenshot({
      path: path.join(artifactDir, `query-profile-${testInfo.project.name}.png`),
      fullPage: true,
    });
    await table.getByRole("button", { name: /no_profile_ui_probe/ }).click();
    await expect(profile).toContainText("No profile was captured for this execution");
    await expect(profile.getByRole("button", { name: "Download profile JSON" })).toHaveCount(0);
  } finally {
    await connection.close();
    await page.goto("about:blank");
    await server.stop();
  }
});

test("status icons animate executing queries and the display preference survives reload", async ({ page }) => {
  expectProblem(page, {
    console:
      /ERR_INCOMPLETE_CHUNKED_ENCODING|Failed to load resource: (The network connection was lost|Connection terminated unexpectedly)/,
    required: false,
  });
  const server = await startOwnServer(["--enable-instrumentation"]);
  try {
    await signIn(page, server.admin, server.url);
    await runSql(page, "SELECT count(*) AS animated_status_probe FROM range(10000000000) a, range(1000) b");
    await expect(page.getByTestId("cancel-button")).toBeVisible();
    await page.getByRole("button", { name: "SQL Monitor", exact: true }).click();
    const table = page.getByTestId("sql-monitor-table");
    const icon = table.getByRole("img", { name: "Executing", exact: true });
    await expect(icon).toBeVisible();
    await expect(icon).toHaveCSS("animation-name", "monitor-spin");
    await expect(icon).toHaveCSS("animation-duration", "1s");
    await page.getByLabel("Status display").selectOption("descriptions");
    await expect(table.locator(".monitor-status-executing")).toHaveText("Executing");
    await expect(table.locator(".monitor-status-executing svg")).toHaveCSS("animation-name", "monitor-spin");
    await page.getByRole("button", { name: "Query", exact: true }).click();
    await page.getByTestId("cancel-button").click();
    await expect(page.getByTestId("results-status")).toContainText("Cancelled after");
    await page.getByRole("button", { name: "SQL Monitor", exact: true }).click();
    await page.reload();
    await expect(page.getByLabel("Status display")).toHaveValue("descriptions");
    await page.getByLabel("Status display").selectOption("icons");
    await page.reload();
    await expect(page.getByLabel("Status display")).toHaveValue("icons");
  } finally {
    await page.goto("about:blank");
    await server.stop();
  }
});
