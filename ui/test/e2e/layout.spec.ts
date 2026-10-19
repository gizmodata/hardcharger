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
