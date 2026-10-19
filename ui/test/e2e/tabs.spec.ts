// Editor tabs: each its own Warp SQL session; new, close, rename, reorder, the cap of 20,
// shortcuts that act on the focused tab, and what survives a reload.
import { activeTab, editorFocused, expect, gridRows, runDone, runSql, setSql, signIn, test } from "./fixtures.ts";

test.beforeEach(async ({ page, harness }) => {
  await signIn(page, harness.admin);
});

const tabNames = (page: import("@playwright/test").Page) => page.getByRole("tab").allTextContents();

test("each tab has its own session, and Ctrl/⌘+Enter runs in the focused tab", async ({ page }) => {
  const sessionOf = async () => {
    await runDone(page, "SELECT HARDCHARGER_CURRENT_SESSION() AS s");
    await expect.poll(async () => (await gridRows(page)).length).toBe(2);
    return (await gridRows(page))[1]![0]!;
  };
  const first = await sessionOf();
  await page.keyboard.press("Alt+N");
  await expect(page.getByRole("tab", { selected: true })).toHaveText("Query 2");
  const second = await sessionOf();
  expect(second).not.toBe(first);
  // The session chip names the same session.
  await expect(activeTab(page).getByTestId("session-chip")).toContainText(second.replace(/^s_/, "").slice(0, 8));

  // Back in the first tab, its own session answers; the shortcut didn't run in the other one.
  await page.getByRole("tab", { name: "Query 1" }).click();
  expect(await sessionOf()).toBe(first);
  await page.getByRole("tab", { name: "Query 2" }).click();
  expect((await gridRows(page))[1]![0]).toBe(second);
});

test("Alt+W closes a tab, asking first when its text isn't saved", async ({ page }) => {
  await editorFocused(page); // the shown tab's editor takes the cursor
  await page.keyboard.press("Alt+N");
  await expect(page.getByRole("tab")).toHaveCount(2);
  await setSql(page, "SELECT 'keep me'");
  let asked = "";
  page.once("dialog", (d) => {
    asked = d.message();
    void d.dismiss();
  });
  await page.keyboard.press("Alt+W");
  await expect.poll(() => asked).toContain("has text you haven't saved");
  await expect(page.getByRole("tab")).toHaveCount(2);
  page.once("dialog", (d) => void d.accept());
  await page.keyboard.press("Alt+W");
  await expect(page.getByRole("tab")).toHaveCount(1);
  // An empty tab closes without a question.
  await editorFocused(page);
  await page.keyboard.press("Alt+N");
  await expect(page.getByRole("tab")).toHaveCount(2);
  await page.getByRole("button", { name: "Close Query 2" }).click();
  await expect(page.getByRole("tab")).toHaveCount(1);
});

test("at most 20 tabs", async ({ page }) => {
  const add = page.getByRole("button", { name: "New tab" });
  for (let i = 1; i < 20; i++) await add.click();
  await expect(page.getByRole("tab")).toHaveCount(20);
  await expect(add).toBeDisabled();
  await editorFocused(page);
  await page.keyboard.press("Alt+N");
  await expect(page.getByRole("tab")).toHaveCount(20);
});

test("double-click renames a tab; dragging reorders", async ({ page }) => {
  await page.getByRole("button", { name: "New tab" }).click();
  await page.getByRole("button", { name: "New tab" }).click();
  expect(await tabNames(page)).toEqual(["Query 1", "Query 2", "Query 3"]);
  await page.getByRole("tab", { name: "Query 2" }).dblclick();
  const name = page.getByLabel("Tab name");
  await name.fill("Revenue");
  await name.press("Enter");
  expect(await tabNames(page)).toEqual(["Query 1", "Revenue", "Query 3"]);
  // Esc keeps the old name.
  await page.getByRole("tab", { name: "Query 3" }).dblclick();
  await page.getByLabel("Tab name").fill("nope");
  await page.getByLabel("Tab name").press("Escape");
  expect(await tabNames(page)).toEqual(["Query 1", "Revenue", "Query 3"]);

  await page.getByRole("tab", { name: "Query 3" }).dragTo(page.getByRole("tab", { name: "Query 1" }));
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
  await expect(page.getByRole("tab")).toHaveCount(2);
  await expect(page.getByRole("tab", { selected: true })).toHaveText("Query 2");
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
