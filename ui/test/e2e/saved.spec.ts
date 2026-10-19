// Saved queries, kept in this browser (IndexedDB): Save (Ctrl/⌘+S) asks a name and a folder
// the first time and updates the same query after; the unsaved dot; Save as; folders, rename,
// move by drag, delete, import .sql files, download (.sql, a folder's .zip); opening a query
// (or going to its tab); the library exported to a file and imported in a fresh browser.
import { readFileSync } from "node:fs";
import { inflateRawSync } from "node:zlib";

import type { Page } from "@playwright/test";

import { activeTab, editorFocused, editorText, expect, setSql, signIn, test, editorTabs } from "./fixtures.ts";

test.beforeEach(async ({ page, harness }) => {
  await signIn(page, harness.admin);
  await editorFocused(page);
  await page.getByRole("tab", { name: "Saved queries" }).click();
});

const panel = (page: Page) => page.getByTestId("saved-queries");
const savedTree = (page: Page) => page.getByRole("tree", { name: "Saved queries" });

async function saveAs(page: Page, name: string, folder?: string) {
  const dialog = page.getByTestId("save-query-dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("Name").fill(name);
  if (folder) await dialog.getByLabel("Folder").selectOption({ label: folder });
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog).toHaveCount(0);
}

async function rowMenu(page: Page, name: string, item: string) {
  const row = savedTree(page).getByRole("treeitem", { name, exact: true }).locator(":scope > .tree-row-wrap");
  await row.hover();
  await row.getByRole("button", { name: `More for ${name}` }).click();
  await page.getByRole("menuitem", { name: item }).click();
}

function unzipNames(bytes: Buffer): Map<string, string> {
  const out = new Map<string, string>();
  let at = 0;
  while (bytes.readUInt32LE(at) === 0x04034b50) {
    const method = bytes.readUInt16LE(at + 8);
    const size = bytes.readUInt32LE(at + 18);
    const n = bytes.readUInt16LE(at + 26);
    const name = bytes.subarray(at + 30, at + 30 + n).toString();
    const body = bytes.subarray(at + 30 + n, at + 30 + n + size);
    out.set(name, (method === 8 ? inflateRawSync(body) : body).toString());
    at += 30 + n + size;
  }
  return out;
}

test("Ctrl/⌘+S saves a tab's SQL; then it updates the same query; the dot shows unsaved changes", async ({ page }) => {
  await setSql(page, "SELECT 'first' AS v");
  await page.keyboard.press("ControlOrMeta+S");
  await saveAs(page, "Daily check");
  await expect(editorTabs(page).getByRole("tab", { selected: true })).toHaveText("Daily check");
  await expect(savedTree(page).getByRole("treeitem", { name: "Daily check" })).toBeVisible();
  await setSql(page, "SELECT 'second' AS v");
  await expect(editorTabs(page).getByRole("tab", { selected: true })).toContainText("●");
  await page.keyboard.press("ControlOrMeta+S"); // no dialog: the same query
  await expect(editorTabs(page).getByRole("tab", { selected: true })).toHaveText("Daily check");
  // Save as… makes a copy under a new name, and the tab follows the copy.
  await activeTab(page).getByRole("button", { name: "Save as…" }).click();
  await saveAs(page, "Daily check copy");
  await expect(editorTabs(page).getByRole("tab", { selected: true })).toHaveText("Daily check copy");
  // Names are unique within a folder.
  await activeTab(page).getByRole("button", { name: "Save as…" }).click();
  const dialog = page.getByTestId("save-query-dialog");
  await dialog.getByLabel("Name").fill("daily CHECK");
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("is already in this folder");
  await dialog.getByRole("button", { name: "Cancel" }).click();

  // Opening a saved query goes to the tab that has it, or opens one.
  await page.getByRole("button", { name: "New tab" }).click();
  await savedTree(page).getByRole("button", { name: "Daily check", exact: true }).click();
  await expect(editorTabs(page).getByRole("tab", { selected: true })).toHaveText("Daily check");
  await expect(editorTabs(page).getByRole("tab")).toHaveCount(3);
  page.once("dialog", (d) => void d.accept());
  await page.keyboard.press("Alt+W");
  await savedTree(page).getByRole("button", { name: "Daily check", exact: true }).click();
  await expect(editorTabs(page).getByRole("tab", { selected: true })).toHaveText("Daily check");
  await expect.poll(() => editorText(page)).toBe("SELECT 'second' AS v");
});

test("folders: new, rename, move by drag, download, delete; import .sql files", async ({ page }, testInfo) => {
  await panel(page).getByRole("button", { name: "New folder" }).click();
  const rename = page.getByLabel("New name");
  await rename.fill("Reports");
  await rename.press("Enter");
  const reports = savedTree(page).getByRole("treeitem", { name: "Reports", exact: true });
  await expect(reports).toBeVisible();
  await reports.locator(":scope > .tree-row-wrap").getByRole("button", { name: "Reports" }).click(); // selected

  // Import two .sql files into the selected folder.
  const a = testInfo.outputPath("revenue.sql");
  const b = testInfo.outputPath("costs.sql");
  const { writeFileSync } = await import("node:fs");
  writeFileSync(a, "SELECT 'revenue';");
  writeFileSync(b, "SELECT 'costs';");
  await page.getByTestId("import-sql").setInputFiles([a, b]);
  await expect(page.getByTestId("saved-notice")).toHaveText("Imported 2 queries.");
  await expect(reports.getByRole("treeitem", { name: "revenue" })).toBeVisible();

  // Download the folder as a .zip of its .sql files.
  const download = page.waitForEvent("download");
  await rowMenu(page, "Reports", "Download as .zip");
  const zip = await download;
  expect(zip.suggestedFilename()).toBe("Reports.zip");
  const files = unzipNames(readFileSync(await zip.path()));
  expect(Object.fromEntries(files)).toEqual({
    "Reports/costs.sql": "SELECT 'costs';",
    "Reports/revenue.sql": "SELECT 'revenue';",
  });

  // Move a query to the top level by dragging it there.
  await reports
    .getByRole("button", { name: "costs", exact: true })
    .dragTo(savedTree(page).getByRole("button", { name: "Top level" }));
  await expect(reports.getByRole("treeitem", { name: "costs" })).toHaveCount(0);
  await expect(savedTree(page).getByRole("treeitem", { name: "costs" })).toBeVisible();

  // Rename, then delete the folder with what's in it, after asking.
  await rowMenu(page, "costs", "Rename");
  await page.getByLabel("New name").fill("costs 2026");
  await page.getByLabel("New name").press("Enter");
  await expect(savedTree(page).getByRole("treeitem", { name: "costs 2026" })).toBeVisible();
  let asked = "";
  page.once("dialog", (d) => {
    asked = d.message();
    void d.accept();
  });
  await rowMenu(page, "Reports", "Delete");
  await expect.poll(() => asked).toBe('Delete the folder "Reports" and the 1 item in it?');
  await expect(reports).toHaveCount(0);
});

test("the library moves to another browser through a file", async ({ page, browser, harness }, testInfo) => {
  await setSql(page, "SELECT 'portable' AS v");
  await page.keyboard.press("ControlOrMeta+S");
  await saveAs(page, "Portable");
  const download = page.waitForEvent("download");
  await panel(page).getByRole("button", { name: "More for saved queries" }).click();
  await page.getByRole("menuitem", { name: "Export all to a file" }).click();
  const file = testInfo.outputPath("library.json");
  await (await download).saveAs(file);
  expect(JSON.parse(readFileSync(file, "utf8")).format).toBe("hardcharger-ui-saved-queries");

  // A fresh browser (its own storage): empty until the file is imported.
  const context = await browser.newContext({ baseURL: harness.url });
  const other = await context.newPage();
  await signIn(other, harness.admin);
  await other.getByRole("tab", { name: "Saved queries" }).click();
  await expect(
    other.getByRole("tree", { name: "Saved queries" }).getByRole("treeitem", { name: "Portable" }),
  ).toHaveCount(0);
  await other.getByTestId("import-library").setInputFiles(file);
  await expect(
    other.getByRole("tree", { name: "Saved queries" }).getByRole("treeitem", { name: "Portable" }),
  ).toBeVisible();
  await context.close();
});
