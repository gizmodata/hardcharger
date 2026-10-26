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
  const about = page.getByRole("region", { name: "About Hardcharger", exact: true });
  await expect(about).toBeVisible();
  await expect(about.getByRole("heading", { name: "Hardcharger™", exact: true })).toHaveCount(0);
  await expect(about.getByRole("link", { name: "Explore Hardcharger" })).toHaveAttribute("href", "https://gizmodata.com/hardcharger");
  await expect(about.getByRole("link", { name: "Visit GizmoData" })).toHaveAttribute("href", "https://gizmodata.com");
  await expect(page.getByRole("link", { name: "by GizmoData™", exact: true })).toHaveAttribute("href", "https://gizmodata.com");
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

test("fixed-choice settings survive filtering and save with their proper types", async ({ page }) => {
  await page.addInitScript(() => {
    const status = {
      running: false, port: 31337, version: "Hardcharger 2.0.0", error: null,
      settings: { port: 31337, start_on_open: true, server_options: {} },
    };
    Object.assign(window, {
      isTauri: true,
      __TAURI_INTERNALS__: {
        transformCallback: () => 1,
        invoke: async (command: string, args: { settings: typeof status.settings }) => {
          if (command === "server_options") return [
            { name: "capture_query_profile", type: "string", group: "Catalog logging", default: "off", description: "Record a query profile", choices: ["off", "standard", "detailed"] },
            { name: "print_queries", type: "boolean", group: "Statements", default: "true", description: "Log every statement" },
          ];
          if (command === "save_settings") status.settings = args.settings;
          return command === "server_status" || command === "save_settings" ? status : false;
        },
      },
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  const search = page.getByRole("searchbox", { name: "Search setting names" });
  await search.fill("profile");
  await page.getByRole("checkbox", { name: "Override", exact: true }).check();
  const profile = page.getByRole("combobox", { name: "capture_query_profile" });
  await expect(profile.locator("option")).toHaveText(["off", "standard", "detailed"]);
  await profile.selectOption("detailed");
  await search.fill("print_queries");
  await page.getByRole("checkbox", { name: "Override", exact: true }).check();
  await page.getByRole("combobox", { name: "print_queries" }).selectOption("false");
  await page.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Settings saved" })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "print_queries" })).toHaveValue("false");
  await search.fill("profile");
  await expect(profile).toHaveValue("detailed");
  await profile.scrollIntoViewIfNeeded();
  await page.screenshot({ path: "artifacts/desktop-setting-choices.png" });
});
