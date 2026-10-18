import { expect, expectProblem, signIn, test } from "./fixtures.ts";

// After an upgrade, the files a page loads lazily are gone. Here the query page's module
// answers 404, as it would for a tab opened before the upgrade: the page reloads after 0, 3
// and 10 seconds, then says a new version is available, while the header and the schema
// browser (outside the page's error boundary) keep working.
test("a lazily loaded page that is gone: reloads with backoff, then a 'new version' notice", async ({
  page,
  harness,
}) => {
  test.setTimeout(90_000);
  const missing = /\/ui\/assets\/QueryPage-[^/]+\.js$/;
  expectProblem(page, { status: 404, url: missing });
  // How each browser reports the failed module import.
  expectProblem(page, {
    console:
      /Failed to load module script|Importing a module script failed|Failed to fetch dynamically imported module/,
    required: false,
  });
  let documents = 0;
  page.on("request", (r) => {
    if (r.resourceType() === "document") documents++;
  });
  // Answered as the server answers a file it doesn't have: 404, never cached (a later
  // build or another replica may have it).
  await page.route(missing, (route) =>
    route.fulfill({ status: 404, contentType: "text/plain", headers: { "Cache-Control": "no-store" }, body: "gone" }),
  );
  await signIn(page, harness.admin);

  await expect(page.getByTestId("stale-build")).toContainText("Loading the latest version", { timeout: 10_000 });
  const notice = page.getByText("A new version of Hardcharger UI is available");
  await expect(notice).toBeVisible({ timeout: 30_000 });
  // The first load, then three automatic reloads.
  expect(documents).toBe(4);
  // Still signed in, and the parts outside the page still work.
  await expect(page.getByTestId("header-user")).toContainText("admin");
  await page.getByRole("button", { name: "Refresh the schema browser" }).click();
  await expect(page.getByRole("treeitem", { name: harness.catalog, exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Sessions" }).click();
  await expect(page.getByTestId("sessions-table")).toBeVisible();
  await page.getByRole("button", { name: "Query" }).click();
  await expect(notice).toBeVisible();

  // The "new version" is deployed: Reload brings the page back.
  await page.unroute(missing);
  await page.getByRole("button", { name: "Reload" }).click();
  await expect(page.locator(".editor-slot .monaco-editor")).toBeVisible();
  await expect(page.getByTestId("stale-build")).toHaveCount(0);
});
