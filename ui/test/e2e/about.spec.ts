// The About dialog's "Open-source licences" link: it opens the build's third-party notices
// (scripts/third-party-notices.mjs), served under /ui like every file of the UI, with each
// bundled package's licence text.
import { expect, signIn, test, watchPage } from "./fixtures.ts";

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
