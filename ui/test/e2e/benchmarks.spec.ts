import { connect } from "@gizmodata/chargewire";
import { activeTab, editorFocused, editorTabs, editorText, expect, runDone, setSql, signIn, test } from "./fixtures.ts";
import { startOwnServer } from "./harness.ts";

test("benchmark selection follows schemas and opens runnable examples without replacing SQL", async ({
  page,
}, testInfo) => {
  const server = await startOwnServer([]);
  const conn = await connect({ url: `${server.url}/chargewire`, ...server.admin });
  try {
    await conn.execute("INSTALL tpch; LOAD tpch; CREATE SCHEMA h; CALL dbgen(sf=0.01, schema='h')");
    await conn.execute("INSTALL tpcds; LOAD tpcds; CREATE SCHEMA ds; CALL dsdgen(sf=0, schema='ds')");
    await signIn(page, server.admin, server.url);
    await editorFocused(page);
    await setSql(page, "SELECT 'keep my work'");
    await expect(page.getByLabel("Benchmark query")).toHaveCount(0);
    await activeTab(page).getByLabel("Schema", { exact: true }).selectOption("h");
    await expect(page.getByLabel("Benchmark query").locator("optgroup option")).toHaveCount(22);
    await page.getByLabel("Benchmark query").selectOption("tpch:7");
    await expect(editorTabs(page).getByRole("tab", { name: /TPC-H Q7/ })).toBeVisible();
    await expect.poll(() => editorText(page)).toContain("shipping.supp_nation");
    await expect(activeTab(page)).toHaveAttribute("data-runs", "0");
    await expect(activeTab(page).getByLabel("Schema", { exact: true })).toHaveValue("h");
    await editorFocused(page);
    await page.keyboard.press("ControlOrMeta+Enter");
    await expect(activeTab(page)).toHaveAttribute("data-runs", "1");
    await expect(activeTab(page).getByTestId("results-grid")).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("benchmark-picker.png"), fullPage: true });
    await editorTabs(page)
      .getByRole("tab", { name: /Query 1/ })
      .click();
    await expect.poll(() => editorText(page)).toBe("SELECT 'keep my work'");
    await activeTab(page).getByLabel("Schema", { exact: true }).selectOption("ds");
    await expect(page.getByLabel("Benchmark query").locator("optgroup option")).toHaveCount(99);
    await page.getByLabel("Benchmark query").selectOption("tpcds:99");
    await expect(editorTabs(page).getByRole("tab", { name: /TPC-DS Q99/ })).toBeVisible();
    await editorFocused(page);
    await page.keyboard.press("ControlOrMeta+Enter");
    await expect(activeTab(page)).toHaveAttribute("data-runs", "1");
    await expect(activeTab(page).locator(".result-error")).toHaveCount(0);
    await runDone(page, "DROP TABLE ds.web_site");
    await expect(page.getByLabel("Benchmark query")).toHaveCount(0);
  } finally {
    await conn.close();
    await page.goto("about:blank");
    await server.stop();
  }
});
