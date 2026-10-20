// About: the stacked bull logo in the theme's variant, the server's details, and the
// copyright and trademark notices; and its "Open-source licences" link, which opens the
// build's third-party notices (scripts/third-party-notices.mjs), served under /ui like every
// file of the UI, with each bundled package's licence text.
import { expect, signIn, test, watchPage } from "./fixtures.ts";

test("About shows the bull logo for the theme, the details, and the notices", async ({ page, harness }, testInfo) => {
  await signIn(page, harness.admin);
  // Playwright's pages prefer the light scheme, so the app starts light.
  await expect(page.getByRole("button", { name: "Dark theme" })).toBeVisible();
  const year = String(new Date().getUTCFullYear());
  for (const theme of ["light", "dark"] as const) {
    if (theme === "dark") await page.getByRole("button", { name: "Dark theme" }).click();
    await page.getByRole("button", { name: "About this server" }).click();
    const about = page.getByRole("dialog", { name: "About this server" });
    await expect(about).toBeVisible();
    // Exactly one logo is shown, the theme's, and it has loaded.
    const logo = about.getByRole("img", { name: "Hardcharger™" });
    await expect(logo).toHaveCount(1);
    await expect(logo).toHaveAttribute("src", new RegExp(`/ui/assets/mascot-stacked-${theme}-[\\w-]+\\.webp$`));
    expect(await logo.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0)).toBe(true);
    await expect(about.getByTestId("about-table")).toContainText("Hardcharger");
    await expect(about.getByText(`© ${year} GizmoData LLC. All rights reserved.`)).toBeVisible();
    await expect(about.getByText("Hardcharger™ and GizmoData™ are trademarks of GizmoData LLC.")).toBeVisible();
    // The copyright line comes before the trademark line.
    const foot = await about.locator(".dialog-foot").innerText();
    expect(foot.indexOf("All rights reserved.")).toBeLessThan(foot.indexOf("are trademarks of"));
    await page.screenshot({ path: testInfo.outputPath(`about-${theme}.png`) });
    await about.getByRole("button", { name: "Close" }).click();
    await expect(about).toHaveCount(0);
  }
});

test("About links the open-source licences, which list every bundled package with its licence text", async ({
  page,
  harness,
}) => {
  await signIn(page, harness.user);
  await page.getByRole("button", { name: "About this server" }).click();
  const dialog = page.getByRole("dialog", { name: "About this server" });
  const link = dialog.getByRole("link", { name: "Open-source licences" });
  await expect(link).toHaveAttribute("href", "/ui/third-party-notices.txt");
  await expect(link).toHaveAttribute("target", "_blank");

  const [notices] = await Promise.all([page.waitForEvent("popup"), link.click()]);
  const done = await watchPage(notices);
  await notices.waitForLoadState();
  expect(new URL(notices.url()).pathname).toBe("/ui/third-party-notices.txt");
  const text = (await notices.locator("body").innerText()).replace(/\r\n/g, "\n");
  expect(text).toMatch(/^Hardcharger web UI \S+: third-party software notices/);
  // The app's own packages, the lazily loaded editor and API page, each with its licence.
  expect(text).toMatch(/^react \d+\.\d+\.\d+\nLicense: MIT$/m);
  expect(text).toMatch(/^monaco-editor \d+\.\d+\.\d+\nLicense: MIT$/m);
  expect(text).toMatch(/^swagger-ui-dist \d+\.\d+\.\d+\nLicense: Apache-2\.0$/m);
  expect(text).toContain("Permission is hereby granted, free of charge"); // MIT's text
  expect(text).toContain("Apache License\n                           Version 2.0, January 2004");
  expect(text).toContain("swagger-ui-es-bundle.js.LICENSE.txt:"); // the bundle's own dependencies
  expect(text).toContain("ThirdPartyNotices.txt:"); // Monaco's
  done();

  // Served as the UI's files are: plain text, under its Content-Security-Policy.
  const response = await page.request.get("/ui/third-party-notices.txt");
  expect(response.status()).toBe(200);
  expect(response.headers()["content-type"]).toBe("text/plain; charset=utf-8");
  expect(response.headers()["content-security-policy"]).toContain("default-src 'self'");
  expect(response.headers()["x-content-type-options"]).toBe("nosniff");
});

// The dialog fits the window: the logo and title at the top and the notices at the bottom stay
// in view, and the details between them scroll inside it. Capabilities wrap between names,
// never inside one, and each server fact is listed once.
for (const viewport of [
  { name: "1280x800", width: 1280, height: 800 },
  { name: "phone", width: 390, height: 844 },
]) {
  test(`About fits a ${viewport.name} window, in both themes`, async ({ page, harness }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await signIn(page, harness.admin);
    for (const theme of ["light", "dark"] as const) {
      if (theme === "dark") await page.getByRole("button", { name: "Dark theme" }).click();
      await page.getByRole("button", { name: "About this server" }).click();
      const about = page.getByRole("dialog", { name: "About this server" });
      await expect(about).toBeVisible();
      await expect(about.getByRole("img", { name: "Hardcharger™" })).toHaveAttribute(
        "src",
        new RegExp(`mascot-stacked-${theme}-`),
      );
      await expect(about.getByTestId("about-table")).toContainText("Hardcharger");
      await page.screenshot({ path: testInfo.outputPath(`about-${viewport.name}-${theme}.png`) });
      await expect(about.getByTestId("about-server-info")).toBeVisible();

      const inView = async (locator: ReturnType<typeof page.locator>) => {
        const box = (await locator.boundingBox())!;
        expect(box.y, `${await locator.innerText()} starts in the window`).toBeGreaterThanOrEqual(0);
        expect(box.y + box.height, `${await locator.innerText()} ends in the window`).toBeLessThanOrEqual(
          viewport.height,
        );
      };
      const dialogBox = (await about.boundingBox())!;
      expect(dialogBox.y + dialogBox.height).toBeLessThanOrEqual(viewport.height);
      await inView(about.getByRole("heading", { name: "About this server" }));
      await inView(about.getByText("Hardcharger™ and GizmoData™ are trademarks of GizmoData LLC."));
      await inView(about.getByText(/All rights reserved\./));
      await inView(about.getByRole("link", { name: "Open-source licences" }));
      expect((await about.getByRole("img", { name: "Hardcharger™" }).boundingBox())!.y).toBeGreaterThanOrEqual(0);

      // Every capability is one unbroken chip inside its cell.
      const chips = about.getByTestId("about-capabilities").getByRole("listitem");
      expect(await chips.count()).toBeGreaterThan(3);
      const broken = await chips.evaluateAll((items) =>
        items
          .filter((li) => {
            const cell = li.closest("td")!.getBoundingClientRect();
            const box = li.getBoundingClientRect();
            const lineHeight = parseFloat(getComputedStyle(li).lineHeight) || 16;
            return box.height > lineHeight * 1.6 || box.right > cell.right + 0.5;
          })
          .map((li) => li.textContent),
      );
      expect(broken, "capabilities broken across lines or out of their cell").toEqual([]);

      // Each fact once: the summary, then only what it doesn't show.
      const more = about.getByTestId("about-server-info");
      await more.getByText("All server info").click();
      const keys = await about.locator("th").allInnerTexts();
      for (const shown of ["server_name", "server_version", "hardcharger_instance_id", "protocol_version"]) {
        expect(keys).not.toContain(shown);
      }
      expect(keys).toContain("duckdb_version");
      expect(new Set(keys).size).toBe(keys.length);

      // The details scroll inside the dialog: their last row comes into view, and the
      // notices are still there.
      const last = more.locator("tr").last();
      await last.scrollIntoViewIfNeeded();
      await inView(last);
      await inView(about.getByText("Hardcharger™ and GizmoData™ are trademarks of GizmoData LLC."));
      await inView(about.getByRole("heading", { name: "About this server" }));
      await page.screenshot({ path: testInfo.outputPath(`about-${viewport.name}-${theme}-all-info.png`) });

      await about.getByRole("button", { name: "Close" }).click();
      await expect(about).toHaveCount(0);
    }
  });
}
