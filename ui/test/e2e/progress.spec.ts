import { activeTab, expect, expectProblem, runDone, runSql, signIn, test } from "./fixtures.ts";

test("query progress appears before rows arrive and cancellation clears it", async ({ page, harness }, testInfo) => {
  await signIn(page, harness.admin);
  await runDone(page, "SET threads=1; SET progress_bar_time=0");
  await runSql(page, "SELECT sum(hash(i)) FROM range(10000000000) t(i)");
  const progress = activeTab(page).getByTestId("query-progress");
  await expect(progress).toBeVisible();
  await expect(progress).toContainText("estimated");
  await expect(progress.getByRole("progressbar")).toHaveAttribute("value", /.+/);
  await page.screenshot({ path: testInfo.outputPath("single-node-progress.png"), fullPage: true });
  expectProblem(page, {
    console:
      /ERR_INCOMPLETE_CHUNKED_ENCODING|Failed to load resource: (The network connection was lost|Connection terminated unexpectedly)/,
    required: false,
  });
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(progress).toHaveCount(0);
  await expect(activeTab(page).getByTestId("results-status")).toContainText("Cancelled");
  await runDone(page, "SELECT 42 AS after_cancel");
  await expect(activeTab(page)).toContainText("after_cancel");
});
