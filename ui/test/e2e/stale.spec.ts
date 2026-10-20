import { expect, expectProblem, signIn, test } from "./fixtures.ts";

// After an upgrade, the files a page loads lazily are gone. Here the query page's module
// answers 404, as it would for a tab opened before the upgrade: the page reloads after 0, 3
// and 10 seconds, then says a new version is available, while the header and the schema
// browser (outside the page's error boundary) keep working.
test("a lazily loaded page that is gone: reloads with backoff, then a 'new version' notice", async ({
  page,
  harness,
  browserName,
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

  // The upgrade finishes: the server now has a new build, whose files have new names, so the
  // reloaded page asks for new URLs (here the same files under new URLs: WebKit may keep a
  // module that failed to load as failed for its URL, as it would in Safari). Reload brings
  // the page back.
  await page.unroute(missing);
  await page.route(`${harness.url}/ui/`, async (route) => {
    const response = await route.fetch();
    const body = (await response.text()).replace(/(\/ui\/assets\/index-[\w-]+\.js)/, "$1?build=2");
    await route.fulfill({ response, body });
  });
  await page.route(/\/ui\/assets\/index-[\w-]+\.js\?build=2$/, async (route) => {
    const response = await route.fetch();
    const body = (await response.text()).replaceAll(/(QueryPage-[\w-]+\.js)/g, "$1?build=2");
    await route.fulfill({ response, body });
  });
  // During this deliberate document replacement, WebKit can report a caught statement
  // cleanup fetch as a pageerror (microsoft/playwright#43120). Allow only that native
  // diagnostic, for this same-origin statement endpoint, from the explicit reload on.
  // HTTP failures, CSP violations and other uncaught errors remain test failures.
  if (browserName === "webkit") {
    const host = new URL(harness.url).host.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    expectProblem(page, {
      console: new RegExp(
        `${host}/warp/v1/sessions/s_[a-z0-9]+/statements/q_[a-z0-9]+ due to access control checks\\.$`,
      ),
      required: false,
    });
  }
  await page.getByTestId("stale-build").getByRole("button", { name: "Reload", exact: true }).click();
  await expect(page.locator(".editor-tab .monaco-editor")).toBeVisible();
  await expect(page.getByTestId("stale-build")).toHaveCount(0);
  // Reload reloaded (WebKit sometimes needs one automatic retry more before the new files load).
  expect(documents).toBeGreaterThanOrEqual(5);
});
