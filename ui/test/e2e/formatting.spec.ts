import { activeTab, editorFocused, editorText, expect, runDone, setSql, signIn, test } from "./fixtures.ts";

test("editor context menu formats SQL and supports Undo, selection and failure", async ({
  page,
  harness,
}, testInfo) => {
  await signIn(page, harness.admin);
  const original = "select Foo, BAR from MyTable";
  await setSql(page, original);
  await activeTab(page).locator(".view-line").first().click({ button: "right" });
  await page.screenshot({ path: testInfo.outputPath("format-sql-menu.png") });
  // Monaco arms menu mouse-up handlers after 100 ms to suppress accidental opening clicks.
  await page.getByRole("menuitem", { name: /Format SQL/ }).click({ delay: 150 });
  await expect.poll(() => editorText(page)).toContain("  , bar");
  await page.screenshot({ path: testInfo.outputPath("formatted-editor.png") });
  await editorFocused(page);
  await page.keyboard.press("ControlOrMeta+Z");
  await expect.poll(() => editorText(page)).toBe(original);

  await setSql(page, "select Foo;\nselect SECOND, THIRD");
  // Explicitly select the first line, leaving the second statement untouched.
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.press("ArrowLeft");
  for (let i = 0; i < "select Foo;".length; i++) await page.keyboard.press("Shift+ArrowRight");
  await page.keyboard.press("Alt+Shift+F");
  await expect.poll(() => editorText(page)).toContain("SELECT\n    foo;");
  await expect.poll(() => editorText(page)).toContain("select SECOND, THIRD");

  await setSql(page, "SELECT …");
  await expect.poll(() => editorText(page)).toBe("SELECT …");
  await page.keyboard.press("Alt+Shift+F");
  await expect(
    activeTab(page)
      .getByRole("status")
      .filter({ hasText: /could not be formatted safely/ }),
  ).toBeVisible();
  await expect.poll(() => editorText(page)).toBe("SELECT …");
  expect(await activeTab(page).getAttribute("data-runs")).toBe("0");
});

test("editor menu is opaque above SQL and results in both themes", async ({ page, harness }, testInfo) => {
  // Keep the toolbar on one row so the menu overlaps the grid on every platform,
  // including Linux WebKit whose menu is shorter than the macOS one.
  await page.setViewportSize({ width: 1600, height: 900 });
  await signIn(page, harness.admin);
  await runDone(page, "SELECT range AS menu_background_probe FROM range(30)");
  await setSql(page, Array.from({ length: 12 }, (_, i) => `-- SQL behind the menu ${i}`).join("\n"));
  const editor = activeTab(page).getByTestId("sql-editor");
  for (const theme of ["light", "dark"] as const) {
    if (theme === "dark") await page.getByRole("button", { name: "Dark theme", exact: true }).click();
    const bounds = await editor.boundingBox();
    expect(bounds).not.toBeNull();
    await editor.click({ button: "right", position: { x: 200, y: bounds!.height - 5 } });
    const menu = page.getByRole("menu");
    await expect(menu).toBeVisible();
    // Wait for Monaco’s short fade-in before checking/capturing the opaque surface.
    await expect(page.locator(".context-view.monaco-menu-container")).toHaveCSS("opacity", "1");
    const colors = await menu.evaluate((el) => {
      const surface = el.closest(".monaco-scrollable-element")!;
      const popup = el.closest(".context-view")!;
      return {
        background: getComputedStyle(surface).backgroundColor,
        foreground: getComputedStyle(surface).color,
        shadow: getComputedStyle(popup).boxShadow,
        popupTop: popup.getBoundingClientRect().top,
      };
    });
    expect(colors.background).toBe(theme === "light" ? "rgb(245, 248, 247)" : "rgb(16, 31, 38)");
    expect(colors.foreground).toBe(theme === "light" ? "rgb(11, 20, 22)" : "rgb(245, 248, 247)");
    expect(colors.shadow).not.toBe("none");
    const menuBounds = await menu.boundingBox();
    const gridBounds = await page.getByTestId("results-grid").boundingBox();
    expect(colors.popupTop).toBeLessThan(bounds!.y + bounds!.height);
    expect(menuBounds!.y + menuBounds!.height).toBeGreaterThan(gridBounds!.y);
    // The popup must receive the hit, even where the grid lies beneath it.
    expect(
      await menu.evaluate((el) => {
        const rect = el.getBoundingClientRect();
        let hit = document.elementFromPoint(rect.x + rect.width / 2, rect.bottom - 12);
        while (hit?.shadowRoot) {
          const next = hit.shadowRoot.elementFromPoint(rect.x + rect.width / 2, rect.bottom - 12);
          if (!next || next === hit) break;
          hit = next;
        }
        return !!hit && el.contains(hit);
      }),
    ).toBe(true);
    const format = page.getByRole("menuitem", { name: /Format SQL/ });
    // Keyboard focus checks the same selection palette without synthetic mouse-delta differences.
    await page.keyboard.press("ArrowDown");
    await expect(format).toHaveCSS("background-color", theme === "light" ? "rgb(225, 234, 232)" : "rgb(26, 48, 57)");
    await page.screenshot({ path: testInfo.outputPath(`opaque-editor-menu-${theme}.png`) });
    await page.keyboard.press("Escape");
    await expect(menu).toHaveCount(0);
  }
});
