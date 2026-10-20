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

/**
 * Each shown suggestion's label as a person sees it: its text, whether it is drawn inside its
 * row (not pushed out of it and clipped) and is what the browser hits at its middle, and its
 * colour against the colour behind it. A row whose label is in the DOM but laid out outside
 * the row passes `toBeVisible()` and `hasText`, and shows the person an empty row.
 */
async function shownLabels(page: Page) {
  return page.evaluate(() => {
    const opaque = (el: Element | null): string => {
      for (let e = el; e; e = e.parentElement) {
        const bg = getComputedStyle(e).backgroundColor;
        if (bg && bg !== "transparent" && !/rgba\(.*,\s*0\)$/.test(bg)) return bg;
      }
      return "";
    };
    const rows = [...document.querySelectorAll(".editor-tab:not([hidden]) .suggest-widget .monaco-list-row")];
    return rows.map((row) => {
      const label = row.querySelector<HTMLElement>(".label-name")!;
      const r = label.getBoundingClientRect();
      const box = row.getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + Math.min(r.width / 2, 6), r.top + r.height / 2);
      return {
        text: label.innerText,
        width: r.width,
        insideRow: r.top >= box.top - 0.5 && r.bottom <= box.bottom + 0.5 && r.left >= box.left && r.right <= box.right,
        hit: hit !== null && label.contains(hit),
        color: getComputedStyle(label).color,
        background: opaque(row),
      };
    });
  });
}

async function expectReadable(page: Page, first: string) {
  await expect
    .poll(async () => (await shownLabels(page)).map((l) => l.text), { message: "the suggestions' labels" })
    .toContain(first);
  for (const l of await shownLabels(page)) {
    expect(l.text, "a label's text").toMatch(/\S/);
    expect(l.width, `"${l.text}" has a width`).toBeGreaterThan(0);
    expect(l.insideRow, `"${l.text}" is drawn inside its row`).toBe(true);
    expect(l.hit, `"${l.text}" is what shows at its place`).toBe(true);
    expect(l.color, `"${l.text}" has a colour of its own`).not.toBe(l.background);
  }
}

test("the suggestions' labels are drawn, before and after a visit to the API page", async ({ page, harness }) => {
  await signIn(page, harness.admin);
  await editorFocused(page);
  await runDone(page, "CREATE OR REPLACE TABLE main.ac_lineitem (l_orderkey BIGINT, l_partkey BIGINT)");

  const check = async () => {
    // As typed (no Ctrl+Space): a table after FROM, then a function.
    await setSql(page, "SELECT * FROM ");
    await page.keyboard.type("ac_lin");
    await expect(page.locator(".editor-tab:not([hidden]) .suggest-widget")).toBeVisible();
    await expectReadable(page, "ac_lineitem");
    await page.keyboard.press("Escape");
    await setSql(page, "SELECT ");
    await page.keyboard.type("versio");
    await expect(page.locator(".editor-tab:not([hidden]) .suggest-widget")).toBeVisible();
    await expectReadable(page, "version");
    await page.keyboard.press("Escape");
  };
  await check();

  // Swagger UI's styles stay on the API page.
  await page.getByRole("button", { name: "API", exact: true }).click();
  await expect(page.getByTestId("api-swagger").locator(".opblock").first()).toBeVisible();
  await page.getByRole("button", { name: "Query", exact: true }).click();
  await expect(page.locator(".editor-tab:not([hidden]) .monaco-editor")).toBeVisible();
  await check();

  await runDone(page, "DROP TABLE main.ac_lineitem");
});
