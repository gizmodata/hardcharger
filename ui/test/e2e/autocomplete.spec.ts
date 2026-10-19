// Autocomplete from the server's metadata, as the user may see it: tables after FROM, a
// catalog's schemas and a schema's tables after a dot, columns through an alias (quoted where
// needed), functions with parentheses, keywords, and nothing inside a string.
import type { Page } from "@playwright/test";

import { editorFocused, editorText, expect, runDone, setSql, signIn, test } from "./fixtures.ts";

const suggestions = (page: Page) => page.locator(".editor-tab:not([hidden]) .suggest-widget .monaco-list-row");
async function suggest(page: Page) {
  await page.keyboard.press("Control+Space");
  await expect(page.locator(".editor-tab:not([hidden]) .suggest-widget")).toBeVisible();
}

test("tables, schemas, columns through an alias, quoting, functions, keywords", async ({ page, harness }) => {
  await signIn(page, harness.admin);
  await editorFocused(page);
  await runDone(
    page,
    'CREATE SCHEMA IF NOT EXISTS ac_s; CREATE OR REPLACE TABLE ac_s.ac_t (id INTEGER, "Mixed Case" VARCHAR, "order" INTEGER)',
    "ControlOrMeta+Shift+Enter",
  );

  // After FROM: the current schema's tables, then its catalog's schemas, then catalogs.
  await setSql(page, "SELECT * FROM ");
  await suggest(page);
  await expect(suggestions(page).filter({ hasText: "sales" }).first()).toBeVisible();
  await expect(suggestions(page).filter({ hasText: harness.catalog }).first()).toBeVisible();
  await page.keyboard.press("Escape");

  // A schema's tables after a dot.
  await setSql(page, "SELECT * FROM ac_s.");
  await suggest(page);
  await expect(suggestions(page).filter({ hasText: "ac_t" })).toBeVisible();
  await page.keyboard.press("Escape");

  // Columns through an alias; a mixed-case name and a keyword are inserted quoted.
  await setSql(page, "SELECT t.Mi FROM ac_s.ac_t t");
  await page.keyboard.press("Home");
  for (let i = 0; i < "SELECT t.Mi".length; i++) await page.keyboard.press("ArrowRight");
  await suggest(page);
  await expect(suggestions(page).filter({ hasText: "Mixed Case" })).toBeVisible();
  await page.keyboard.press("Enter");
  await expect.poll(() => editorText(page)).toBe('SELECT t."Mixed Case" FROM ac_s.ac_t t');
  await setSql(page, "SELECT t.ord FROM ac_s.ac_t t");
  await page.keyboard.press("Home");
  for (let i = 0; i < "SELECT t.ord".length; i++) await page.keyboard.press("ArrowRight");
  await suggest(page);
  await expect(
    suggestions(page)
      .filter({ hasText: /^order/ })
      .first(),
  ).toBeVisible();
  await page.keyboard.press("Enter");
  await expect.poll(() => editorText(page)).toBe('SELECT t."order" FROM ac_s.ac_t t');

  // A function is inserted with its parentheses; keywords are offered.
  await setSql(page, "SELECT string_agg");
  await suggest(page);
  await expect(
    suggestions(page)
      .filter({ hasText: /^string_agg/ })
      .first(),
  ).toBeVisible();
  await page.keyboard.press("Enter");
  await expect.poll(() => editorText(page)).toBe("SELECT string_agg()");
  await setSql(page, "SELECT 1 GROU");
  await suggest(page);
  await expect(suggestions(page).filter({ hasText: /^GROUP$/ })).toBeVisible();
  await page.keyboard.press("Escape");

  // Nothing inside a string.
  await setSql(page, "SELECT 'FROM ac_s.");
  await page.keyboard.press("Control+Space");
  await page.waitForTimeout(500);
  await expect(suggestions(page).filter({ hasText: "ac_t" })).toHaveCount(0);
  await page.keyboard.press("Escape");
});

test("a user is offered only what access control lets them see", async ({ page, harness }) => {
  await signIn(page, harness.user);
  await editorFocused(page);
  await setSql(page, "SELECT * FROM ");
  await suggest(page);
  await expect(suggestions(page).filter({ hasText: "orders" }).first()).toBeVisible();
  await expect(suggestions(page).filter({ hasText: "payroll" })).toHaveCount(0);
  await expect(suggestions(page).filter({ hasText: harness.hiddenCatalog })).toHaveCount(0);
});
