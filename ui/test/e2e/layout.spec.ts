// The page on a phone-sized screen: the side panel (Saved queries and Data) sits above the
// editor at full width, without the bar that resizes it, and a note says a larger screen is
// easier; its × hides the note in this browser. And the help that says what this browser keeps.
import { activeTab, editorFocused, expect, gridRows, runDone, signIn, test } from "./fixtures.ts";

test("on a narrow screen the side panel sits above the editor, and the note hides for good", async ({
  page,
  harness,
}) => {
  await signIn(page, harness.admin);
  await editorFocused(page);
  // A wide window: the panel is beside the editor, and there is no note.
  await expect(page.getByTestId("narrow-note")).toBeHidden();
  await expect(page.getByTestId("side-panel-resize")).toBeVisible();

  await page.setViewportSize({ width: 390, height: 844 });
  const panel = (await page.getByTestId("side-panel").boundingBox())!;
  const editor = (await activeTab(page).locator(".editor-area").boundingBox())!;
  expect(panel.width).toBe(390);
  expect(panel.y + panel.height).toBeLessThanOrEqual(editor.y);
  await expect(page.getByTestId("side-panel-resize")).toBeHidden();
  // Both of the panel's tabs work there, and so does the editor.
  await page.getByRole("tab", { name: "Saved queries" }).click();
  await expect(page.getByTestId("saved-hint")).toContainText("Kept in this browser, not on the server.");
  await page.getByRole("tab", { name: "Data" }).click();
  await expect(page.getByRole("treeitem", { name: new RegExp(`^${harness.catalog}`) })).toBeVisible();
  await runDone(page, "SELECT 42 AS answer");
  expect(await gridRows(page)).toEqual([["answer"], ["42"]]);

  const note = page.getByTestId("narrow-note");
  await expect(note).toContainText("easier to use on a larger screen");
  await note.getByRole("button", { name: "Hide this note" }).click();
  await expect(note).toHaveCount(0);
  await page.reload();
  await editorFocused(page); // (the user chip is hidden on a narrow screen)
  await expect(page.getByTestId("narrow-note")).toHaveCount(0);
});

test("the help says how sessions and transactions work, and what stays in this browser", async ({ page, harness }) => {
  await signIn(page, harness.admin);
  await page.getByRole("button", { name: "How the editor works" }).click();
  const help = page.getByTestId("help-body");
  // The shared test server keeps the default idle timeout.
  await expect(help).toContainText("ends after 15 minutes idle (this server's setting)");
  await expect(help).toContainText("Saved queries and your open tabs (with their text) stay in this browser");
  await page.getByRole("dialog").getByRole("button", { name: "Close" }).click();
  await expect(help).toHaveCount(0);
});

// The desktop app's text size zooms the page (#55): at 150% and 200% of the SQL workspace's
// smallest window (880×600) the page is laid out 586×400 and 440×300 CSS pixels. The editor
// gives way so the toolbar and the results are still there to reach, and nothing scrolls
// sideways.
for (const zoom of [1.5, 2]) {
  test(`a large text size in the smallest workspace window (${zoom * 100}%) keeps Run and the results reachable`, async ({
    browser,
    harness,
  }) => {
    const context = await browser.newContext({
      viewport: { width: Math.floor(880 / zoom), height: Math.floor(600 / zoom) },
      deviceScaleFactor: zoom,
    });
    const page = await context.newPage();
    await page.goto("/ui/");
    await page.getByLabel("User name").fill(harness.admin.username);
    await page.getByLabel("Password").fill(harness.admin.password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByTestId("header-user")).toBeAttached();
    await runDone(page, "SELECT range AS n, 'row ' || range AS label FROM range(30)");
    const tab = activeTab(page);
    const run = tab.getByRole("button", { name: "Run", exact: true });
    await run.scrollIntoViewIfNeeded();
    await expect(run).toBeInViewport();
    const grid = tab.getByTestId("results-grid");
    await grid.scrollIntoViewIfNeeded();
    await expect(grid).toBeInViewport();
    expect((await grid.boundingBox())!.height).toBeGreaterThan(30);
    expect((await gridRows(page))[1]).toEqual(["0", "row 0"]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
    await context.close();
  });
}
