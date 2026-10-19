// About: the stacked bull logo in the theme's variant, the server's details, and the
// copyright and trademark notices.
import { expect, signIn, test } from "./fixtures.ts";

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
