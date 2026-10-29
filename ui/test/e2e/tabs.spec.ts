// Editor tabs: each its own Chargewire session; new, close, rename, reorder, the cap of 20,
// shortcuts that act on the focused tab, and what survives a reload.
import {
  activeTab,
  editorFocused,
  expect,
  gridRows,
  runDone,
  runSql,
  setSql,
  signIn,
  test,
  editorTabs,
} from "./fixtures.ts";

test.beforeEach(async ({ page, harness }) => {
  await signIn(page, harness.admin);
});

const tabNames = (page: import("@playwright/test").Page) => editorTabs(page).getByRole("tab").allTextContents();

test("each tab has its own session, and Ctrl/⌘+Enter runs in the focused tab", async ({ page }) => {
  const sessionOf = async () => {
    await runDone(page, "SELECT HARDCHARGER_CURRENT_SESSION() AS s");
    await expect.poll(async () => (await gridRows(page)).length).toBe(2);
    return (await gridRows(page))[1]![0]!;
  };
  const first = await sessionOf();
  await page.keyboard.press("Alt+N");
  await expect(editorTabs(page).getByRole("tab", { selected: true })).toHaveText("Query 2");
  const second = await sessionOf();
  expect(second).not.toBe(first);
  // The session chip names the same session.
  await expect(activeTab(page).getByTestId("session-chip")).toContainText(second.replace(/^s_/, "").slice(0, 8));

  // Back in the first tab, its own session answers; the shortcut didn't run in the other one.
  await editorTabs(page).getByRole("tab", { name: "Query 1" }).click();
  expect(await sessionOf()).toBe(first);
  await editorTabs(page).getByRole("tab", { name: "Query 2" }).click();
  expect((await gridRows(page))[1]![0]).toBe(second);
});

test("Alt+W closes a tab, asking first when its text isn't saved", async ({ page }) => {
  await editorFocused(page); // the shown tab's editor takes the cursor
  await page.keyboard.press("Alt+N");
  await expect(editorTabs(page).getByRole("tab")).toHaveCount(2);
  await setSql(page, "SELECT 'keep me'");
  let asked = "";
  page.once("dialog", (d) => {
    asked = d.message();
    void d.dismiss();
  });
  await page.keyboard.press("Alt+W");
  await expect.poll(() => asked).toContain("has text you haven't saved");
  await expect(editorTabs(page).getByRole("tab")).toHaveCount(2);
  page.once("dialog", (d) => void d.accept());
  await page.keyboard.press("Alt+W");
  await expect(editorTabs(page).getByRole("tab")).toHaveCount(1);
  // An empty tab closes without a question.
  await editorFocused(page);
  await page.keyboard.press("Alt+N");
  await expect(editorTabs(page).getByRole("tab")).toHaveCount(2);
  await page.getByRole("button", { name: "Close Query 2" }).click();
  await expect(editorTabs(page).getByRole("tab")).toHaveCount(1);
});

test("at most 20 tabs", async ({ page }) => {
  const add = page.getByRole("button", { name: "New tab" });
  for (let i = 1; i < 20; i++) await add.click();
  await expect(editorTabs(page).getByRole("tab")).toHaveCount(20);
  await expect(add).toBeDisabled();
  await editorFocused(page);
  await page.keyboard.press("Alt+N");
  await expect(editorTabs(page).getByRole("tab")).toHaveCount(20);
});

test("double-click renames a tab; dragging reorders", async ({ page }) => {
  await page.getByRole("button", { name: "New tab" }).click();
  await page.getByRole("button", { name: "New tab" }).click();
  expect(await tabNames(page)).toEqual(["Query 1", "Query 2", "Query 3"]);
  await editorTabs(page).getByRole("tab", { name: "Query 2" }).dblclick();
  const name = page.getByLabel("Tab name");
  await name.fill("Revenue");
  await name.press("Enter");
  expect(await tabNames(page)).toEqual(["Query 1", "Revenue", "Query 3"]);
  // Esc keeps the old name.
  await editorTabs(page).getByRole("tab", { name: "Query 3" }).dblclick();
  await page.getByLabel("Tab name").fill("nope");
  await page.getByLabel("Tab name").press("Escape");
  expect(await tabNames(page)).toEqual(["Query 1", "Revenue", "Query 3"]);

  await editorTabs(page)
    .getByRole("tab", { name: "Query 3" })
    .dragTo(editorTabs(page).getByRole("tab", { name: "Query 1" }));
  expect(await tabNames(page)).toEqual(["Query 3", "Query 1", "Revenue"]);
});

test("tabs come back after a reload with their text, catalog, schema and row limit", async ({ page }) => {
  await page.getByRole("button", { name: "New tab" }).click();
  await setSql(page, "SELECT 'second tab'");
  await activeTab(page).getByLabel("Row limit").selectOption("5000");
  await setSql(page, "CREATE SCHEMA IF NOT EXISTS restored_s; USE restored_s");
  await page.keyboard.press("ControlOrMeta+Shift+Enter");
  await expect(activeTab(page).getByLabel("Schema")).toHaveValue("restored_s");
  await page.reload();
  await expect(editorTabs(page).getByRole("tab")).toHaveCount(2);
  await expect(editorTabs(page).getByRole("tab", { selected: true })).toHaveText("Query 2");
  await expect(activeTab(page).locator(".view-lines")).toContainText("USE restored_s");
  await expect(activeTab(page).getByLabel("Row limit")).toHaveValue("5000");
  await expect(activeTab(page).getByLabel("Schema")).toHaveValue("restored_s");
  // The restored tab's session starts there.
  await runSql(page, "SELECT current_schema() AS s");
  await expect.poll(async () => (await gridRows(page))[1]).toEqual(["restored_s"]);
});

test("the editor's height: drag, keyboard, double-click to reset, remembered", async ({ page }) => {
  const handle = page.getByTestId("editor-resize").first();
  const height = async () => (await page.locator(".editor-tab:not([hidden]) .editor-area").boundingBox())!.height;
  const before = await height();
  await handle.focus();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Shift+ArrowDown");
  await expect.poll(height).toBe(before + 96);
  const box = (await handle.boundingBox())!;
  const y = box.y + box.height / 2;
  await page.mouse.move(box.x + box.width / 2, y);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2, y - 60, { steps: 4 });
  await page.mouse.up();
  await expect.poll(height).toBe(before + 36);
  await page.reload();
  await expect.poll(height).toBe(before + 36);
  await page.getByTestId("editor-resize").first().dblclick();
  await expect.poll(height).toBe(before);
});

test("every shortcut acts on the shown tab, not on the tab opened last", async ({ page }) => {
  // Each tab's editor has its own copy of the shortcuts; Monaco's addCommand would have let
  // the last editor mounted answer them in every tab.
  await editorFocused(page);
  await page.keyboard.press("Alt+N");
  await editorFocused(page);
  await page.keyboard.press("Alt+N");
  await expect(editorTabs(page).getByRole("tab")).toHaveCount(3);
  await editorTabs(page).getByRole("tab", { name: "Query 1" }).click();
  await editorFocused(page);

  // Run all (Shift+Ctrl/⌘+Enter) runs the first tab's script, in the first tab.
  await setSql(page, "SELECT 1 AS a;\nSELECT 2 AS b;");
  await page.keyboard.press("ControlOrMeta+Shift+Enter");
  const tab = activeTab(page);
  await expect(tab.getByRole("tablist", { name: "Result sets" }).getByRole("tab", { name: /^Result/ })).toHaveCount(2);
  expect(await gridRows(page)).toEqual([["b"], ["2"]]);
  for (const other of ["Query 2", "Query 3"]) {
    await editorTabs(page).getByRole("tab", { name: other }).click();
    await expect(activeTab(page).getByTestId("results-grid")).toHaveCount(0);
  }

  // Ctrl+Enter, on a Mac too (where ⌘ is the documented key), runs in the first tab.
  await editorTabs(page).getByRole("tab", { name: "Query 1" }).click();
  await editorFocused(page);
  await runDone(page, "SELECT 'one' AS t", "Control+Enter");
  expect(await gridRows(page)).toEqual([["t"], ["one"]]);

  // Ctrl/⌘+S saves the first tab's SQL under its name.
  await page.keyboard.press("ControlOrMeta+S");
  const dialog = page.getByTestId("save-query-dialog");
  await dialog.getByLabel("Name").fill("First tab's script");
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(editorTabs(page).getByRole("tab", { selected: true })).toHaveText("First tab's script");

  // Alt+W closes the first tab (saved, so it doesn't ask), not the last one.
  await editorFocused(page);
  await page.keyboard.press("Alt+W");
  expect(await tabNames(page)).toEqual(["Query 2", "Query 3"]);
});
