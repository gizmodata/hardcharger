// Public documentation captures: a throwaway server and synthetic data only. Never use a
// user's running desktop database. Playwright keeps these images as review artifacts.
import { connect } from "@gizmodata/chargewire";

import { expect, runDone, signIn, test } from "./fixtures.ts";
import { startOwnServer } from "./harness.ts";

test("documentation screenshots of SQL Monitor and Server Configuration", async ({ page }, testInfo) => {
  const server = await startOwnServer(["--enable-instrumentation", "--max-concurrent-statements", "4"]);
  const connection = await connect({ url: `${server.url}/chargewire`, ...server.admin });
  try {
    await connection.execute("CREATE SCHEMA sales");
    await connection.execute(
      "CREATE TABLE sales.orders AS SELECT i AS id, 'Customer ' || (i % 7) AS customer, " +
        "(i * 1.25)::DECIMAL(12,2) AS amount FROM range(2500) t(i)",
    );
    await connection.query(
      "SELECT customer, sum(amount) AS total FROM sales.orders GROUP BY customer ORDER BY total DESC",
    );
    await connection.query("SELECT count(*) AS orders FROM sales.orders");
    try {
      await connection.query("SELECT missing_column FROM sales.orders");
    } catch {
      // Show an actual failed query alongside successful queries in the monitor.
    }
    await signIn(page, server.admin, server.url);
    await runDone(page, "SELECT count(*) AS orders FROM sales.orders");
    await page.getByRole("button", { name: "Hide the side panel", exact: true }).click();
    await page.getByRole("button", { name: "SQL Monitor", exact: true }).click();
    await page.getByLabel("Filter monitored queries").fill("sales.orders");
    const monitor = page.getByTestId("sql-monitor-table");
    await expect(monitor).toContainText("missing_column");
    await expect(monitor).toContainText("Succeeded");
    await expect(monitor).toContainText("Failed");
    await page.getByRole("button", { name: "Pause", exact: true }).click();
    await page.screenshot({ path: testInfo.outputPath("web-ui-monitor-light.png") });
    await page.getByRole("button", { name: "Dark theme" }).click();
    await page.screenshot({ path: testInfo.outputPath("web-ui-monitor-dark.png") });
    await page.getByRole("button", { name: "Light theme" }).click();

    await page.getByRole("button", { name: "Server Configuration", exact: true }).click();
    await page.getByLabel("Filter server settings").fill("query");
    const timeout = page.locator('tr[data-setting="hardcharger.query_timeout"]');
    await timeout.getByRole("button", { name: "Edit session hardcharger.query_timeout", exact: true }).click();
    await page.getByLabel("New setting value").fill("60");
    await page.getByRole("button", { name: "Apply change", exact: true }).click();
    await expect(timeout.getByTestId("setting-session-value")).toContainText("Session override");
    await expect(timeout.getByTestId("setting-session-value")).toContainText("60");
    await page.screenshot({ path: testInfo.outputPath("web-ui-configuration-light.png") });
    await page.getByRole("button", { name: "Dark theme" }).click();
    await page.screenshot({ path: testInfo.outputPath("web-ui-configuration-dark.png") });
  } finally {
    await connection.close();
    await page.goto("about:blank");
    await server.stop();
  }
});

test("documentation screenshots of the results' Map view", async ({ page }, testInfo) => {
  const server = await startOwnServer([]);
  const connection = await connect({ url: `${server.url}/chargewire`, ...server.admin });
  try {
    await connection.execute(
      `CREATE TABLE cities AS SELECT * FROM (VALUES
        ('Charlotte', 'United States', 911311, 'POINT (-80.8431 35.2271)'),
        ('New York', 'United States', 8258035, 'POINT (-74.006 40.7128)'),
        ('Mexico City', 'Mexico', 9209944, 'POINT (-99.1332 19.4326)'),
        ('São Paulo', 'Brazil', 11451999, 'POINT (-46.6333 -23.5505)'),
        ('London', 'United Kingdom', 8866180, 'POINT (-0.1276 51.5072)'),
        ('Paris', 'France', 2102650, 'POINT (2.3522 48.8566)'),
        ('Lagos', 'Nigeria', 15388000, 'POINT (3.3792 6.5244)'),
        ('Mumbai', 'India', 12442373, 'POINT (72.8777 19.076)'),
        ('Tokyo', 'Japan', 14094034, 'POINT (139.6917 35.6895)'),
        ('Sydney', 'Australia', 5450496, 'POINT (151.2093 -33.8688)')
      ) t(name, country, population, wkt)`,
    );
    await signIn(page, server.admin, server.url);
    await runDone(
      page,
      "SELECT name, country, population, wkt::GEOMETRY AS geom FROM cities\n" +
        "UNION ALL SELECT 'Charlotte to London', 'route', NULL,\n" +
        "  'LINESTRING (-80.8431 35.2271, -40 50, -0.1276 51.5072)'::GEOMETRY",
    );
    await page.getByRole("button", { name: "Hide the side panel", exact: true }).click();
    await page.getByTestId("results-view-map").click();
    const map = page.getByTestId("results-map");
    await expect(map.getByTestId("map-legend")).toHaveText(
      "11 geometries · No CRS on the column: drawn as longitude, latitude",
    );
    await expect(map.locator("path.map-land").first()).toBeAttached();
    await page.screenshot({ path: testInfo.outputPath("web-ui-map-light.png") });
    await page.getByRole("button", { name: "Dark theme" }).click();
    await page.screenshot({ path: testInfo.outputPath("web-ui-map-dark.png") });
    await page.getByRole("button", { name: "Light theme" }).click();
  } finally {
    await connection.close();
    await page.goto("about:blank");
    await server.stop();
  }
});
