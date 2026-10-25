import { test, expect } from "@playwright/test";

test("branded controller layouts and preferences stay usable in both themes", async ({ page }) => {
  const failures: string[] = [];
  page.on("pageerror", e => failures.push(e.message));
  await page.goto("/?preview=1");
  await expect(page.getByRole("heading", { name: "Local server", exact: true })).toBeVisible();
  await expect(page.getByText("Design preview · sample status")).toBeVisible();
  await expect(page.getByRole("button", { name: "Open SQL workspace", exact: true })).toBeDisabled();
  await expect(page.locator(".brand-light").first()).toBeVisible();
  await page.screenshot({ path: "artifacts/desktop-light.png" });
  await page.getByRole("button", { name: "Change color theme" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator(".brand-dark").first()).toBeVisible();
  await page.screenshot({ path: "artifacts/desktop-dark.png" });
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByRole("spinbutton", { name: "Port" })).toBeVisible();
  await expect(page.getByRole("checkbox", { name: /Launch at login/ })).toBeVisible();
  await page.getByRole("button", { name: "Logs", exact: true }).click();
  await expect(page.getByText("Design preview. Live server logs appear in the desktop app.")).toBeVisible();
  await page.getByRole("button", { name: "About Hardcharger" }).click();
  await expect(page.getByRole("heading", { name: "Hardcharger™", exact: true })).toBeVisible();
  await page.setViewportSize({ width: 860, height: 640 });
  await page.getByRole("button", { name: "Local server", exact: true }).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(failures).toEqual([]);
});

test("an ordinary browser never reports a live desktop server", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("Open the Hardcharger desktop app");
  await expect(page.getByRole("button", { name: "Start server", exact: true })).toBeDisabled();
});
