// Without IndexedDB (a private window, blocked site data) the saved-queries panel says it can't
// keep them, Save says so, and everything else works.
import { activeTab, editorFocused, expect, setSql, signIn, test } from "./fixtures.ts";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, "indexedDB", {
      get() {
        throw new DOMException("blocked", "SecurityError");
      },
    });
  });
});

test("the panel says it can't keep saved queries; everything else works", async ({ page, harness }) => {
  await signIn(page, harness.admin);
  await editorFocused(page);
  await page.getByRole("tab", { name: "Saved queries" }).click();
  const panel = page.getByTestId("saved-queries");
  await expect(panel).toContainText("doesn't let the page keep saved queries");
  await expect(panel.getByRole("button", { name: "New folder" })).toBeDisabled();
  let said = "";
  page.once("dialog", (d) => {
    said = d.message();
    void d.accept();
  });
  await setSql(page, "SELECT 'still works' AS v");
  await page.keyboard.press("ControlOrMeta+S");
  await expect.poll(() => said).toContain("doesn't let the page keep saved queries");
  await page.keyboard.press("ControlOrMeta+Enter");
  await expect(activeTab(page).getByTestId("results-status")).toContainText("1 row in");
});
