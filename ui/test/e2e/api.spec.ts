// The API page: the server's own OpenAPI document in Swagger UI, whose "Try it out" sends the
// signed-in user's token to this origin only. Like every test, it fails on a console error,
// a failed request or a Content-Security-Policy violation (fixtures.ts).
import type { Locator, Page, Request } from "@playwright/test";

import { expect, signIn, test } from "./fixtures.ts";

/** The access token the page holds (this tab's sessionStorage). */
async function pageToken(page: Page): Promise<string> {
  return page.evaluate(() => {
    const raw = window.sessionStorage.getItem("hardcharger-ui:sign-in");
    return raw ? (JSON.parse(raw) as { token: string }).token : "";
  });
}

/** Opens an operation's block, presses Try it out, and returns the block. */
async function tryOut(page: Page, tag: string, operationId: string) {
  const block = page.locator(`#operations-${tag}-${operationId}`);
  await block.locator(".opblock-summary-control").click();
  await block.getByRole("button", { name: "Try it out" }).click();
  return block;
}

/** The status of the answer an operation's Execute got. */
const status = (block: Locator) => block.locator(".live-responses-table tbody .response-col_status").first();
/** The body of that answer, as shown. */
const shownBody = (block: Locator) =>
  block.locator(".live-responses-table tbody .response-col_description .highlight-code pre").first();

/** Presses Execute and waits for the answer to the request it sends to `path`. */
async function execute(page: Page, block: Locator, path: RegExp): Promise<Request> {
  const sent = page.waitForRequest((r) => path.test(new URL(r.url()).pathname));
  await block.getByRole("button", { name: "Execute" }).click();
  const request = await sent;
  await expect(status(block)).toBeVisible();
  return request;
}

test("lists the server's operations, and Try it out runs as the signed-in user", async ({ page, harness }) => {
  const origins = new Set<string>();
  page.on("request", (r) => origins.add(new URL(r.url()).origin));
  await signIn(page, harness.user);
  await page.getByRole("button", { name: "API", exact: true }).click();
  await expect(page).toHaveURL(/\/ui\/api$/);
  const ui = page.getByTestId("api-swagger");
  await expect(ui.locator(".info .title")).toContainText("Chargewire");
  // Every endpoint group, and operations in each.
  for (const tag of ["Discovery", "Authentication", "Sessions", "Statements", "Metadata"]) {
    await expect(ui.locator(`#operations-tag-${tag}`)).toBeVisible();
  }
  await expect(ui.locator(".opblock")).not.toHaveCount(0);
  expect(await ui.locator(".opblock").count()).toBeGreaterThan(20);
  // No server switcher, no Authorize button: requests go here, with the sign-in.
  await expect(ui.locator(".servers")).toHaveCount(0);
  await expect(ui.locator(".authorize")).toHaveCount(0);

  const token = await pageToken(page);
  expect(token).not.toBe("");

  // Open a session: the request carries the page's token, which the user never typed.
  const create = await tryOut(page, "Sessions", "createSession");
  await create.locator("textarea.body-param__text").fill("{}");
  const created = await execute(page, create, /^\/chargewire\/v1\/sessions$/);
  expect(
    (await created.headerValue("authorization")) === `Bearer ${token}`,
    "the request carries the page's token",
  ).toBe(true);
  await expect(status(create)).toHaveText("201");
  const body = await shownBody(create).innerText();
  const sessionId = (JSON.parse(body) as { session_id: string }).session_id;
  expect(sessionId).toMatch(/\S/);
  // What the page shows (Curl, Request URL) is the request without the token.
  expect((await create.locator(".curl-command").innerText()).includes(token), "the token is not shown").toBe(false);
  await expect(create.locator(".curl-command")).not.toContainText("Authorization");

  // A GET with it: 200, as this user.
  const read = await tryOut(page, "Sessions", "getSession");
  await read.locator('tr[data-param-name="session_id"] input').fill(sessionId);
  const got = await execute(page, read, /^\/chargewire\/v1\/sessions\/[^/]+$/);
  expect((await got.headerValue("authorization")) === `Bearer ${token}`, "the request carries the page's token").toBe(
    true,
  );
  await expect(status(read)).toHaveText("200");
  await expect(shownBody(read)).toContainText(sessionId);

  // An Arrow answer is a file to download, not bytes printed as text.
  const catalogs = await tryOut(page, "Metadata", "getCatalogs");
  await catalogs.locator('tr[data-param-name="session_id"] input').fill(sessionId);
  await execute(page, catalogs, /\/metadata\/catalogs$/);
  await expect(status(catalogs)).toHaveText("200");
  const link = catalogs.getByRole("link", { name: "Download file" });
  await expect(link).toHaveAttribute("download", "result.arrows");
  await expect(catalogs.locator(".live-responses-table")).not.toContainText("ARROW1");
  const [download] = await Promise.all([page.waitForEvent("download"), link.click()]);
  expect(download.suggestedFilename()).toBe("result.arrows");

  // Close the session again (204), so no test after this one finds it.
  const close = await tryOut(page, "Sessions", "closeSession");
  await close.locator('tr[data-param-name="session_id"] input').fill(sessionId);
  await execute(page, close, /^\/chargewire\/v1\/sessions\/[^/]+$/);
  await expect(status(close)).toHaveText("204");

  // Nothing went anywhere but this page's origin.
  expect([...origins]).toEqual([new URL(harness.url).origin]);
});

test("follows the dark theme", async ({ page, harness }) => {
  await signIn(page, harness.user);
  await page.getByRole("button", { name: "API", exact: true }).click();
  const ui = page.getByTestId("api-swagger");
  await expect(ui.locator(".opblock").first()).toBeVisible();
  await page.getByRole("button", { name: "Dark theme" }).click();
  // Swagger UI's dark theme is on its own shadow host, not on the page.
  await expect(ui).toHaveClass(/\bdark-mode\b/);
  await expect(page.locator("html")).not.toHaveClass(/\bdark-mode\b/);
  // Readable: light text on the app's dark background (Swagger UI fades colours in over 0.2 s).
  const luminance = (selector: string, property: "color" | "backgroundColor") =>
    page
      .locator(selector)
      .first()
      .evaluate((el, p) => {
        const rgb = (getComputedStyle(el)[p].match(/\d+(\.\d+)?/g) ?? []).slice(0, 3).map(Number);
        return rgb.reduce((a, b) => a + b, 0) / 3;
      }, property);
  await expect.poll(() => luminance(".api-swagger .opblock-tag", "color")).toBeGreaterThan(180);
  await expect.poll(() => luminance(".api-swagger .opblock-summary-description", "color")).toBeGreaterThan(180);
  expect(await luminance("body", "backgroundColor")).toBeLessThan(60);
  await page.getByRole("button", { name: "Light theme" }).click();
  await expect(ui).not.toHaveClass(/\bdark-mode\b/);
});

test("Swagger UI's styles stay inside the API page, and the page's stay out of it", async ({ page, harness }) => {
  await signIn(page, harness.user);
  await page.getByRole("button", { name: "API", exact: true }).click();
  const ui = page.getByTestId("api-swagger");
  await expect(ui.locator(".opblock").first()).toBeVisible();
  // Swagger UI is in a shadow root with its stylesheet; the page's own stylesheets have none
  // of its rules (they would style other elements with its generic class names: .main, .info).
  expect(await ui.evaluate((el) => el.shadowRoot?.querySelector("style")?.textContent?.includes(".opblock"))).toBe(
    true,
  );
  const leaked = await page.evaluate(() =>
    [...document.styleSheets].flatMap((sheet) =>
      [...sheet.cssRules].map((r) => r.cssText).filter((t) => /\.swagger-ui|\.opblock/.test(t)),
    ),
  );
  expect(leaked).toEqual([]);
  // ...and the app's generic rules don't reach Swagger UI: its .btn (the app's makes a button
  // a 30 px inline-flex box, Swagger UI's doesn't say).
  await ui.locator(".opblock-summary-control").first().click();
  const swaggerBtn = ui.getByRole("button", { name: "Try it out" }).first();
  await expect(swaggerBtn).toBeVisible();
  expect(await swaggerBtn.evaluate((el) => getComputedStyle(el).display)).not.toBe("inline-flex");
});

test("says so when the server doesn't describe its API", async ({ page, harness }) => {
  // A server without the capability: discovery as this one's, minus `openapi`.
  await page.route(/\/chargewire$/, async (route) => {
    const resp = await route.fetch();
    const doc = (await resp.json()) as { capabilities?: string[] };
    doc.capabilities = (doc.capabilities ?? []).filter((c) => c !== "openapi");
    await route.fulfill({ response: resp, json: doc });
  });
  const asked: string[] = [];
  page.on("request", (r) => asked.push(new URL(r.url()).pathname));
  await signIn(page, harness.user);
  await page.getByRole("button", { name: "API", exact: true }).click();
  await expect(page.getByTestId("api-missing")).toContainText("doesn't publish a description of its API");
  await expect(page.getByTestId("api-swagger")).toHaveCount(0);
  expect(asked).not.toContain("/chargewire/v1/openapi.json");
});
