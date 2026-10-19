import { expect, expectProblem, gridRows, runSql, signIn, test } from "./fixtures.ts";

test("a wrong password is refused with the server's reason, and the form stays", async ({ page, harness }) => {
  expectProblem(page, { status: 401, url: /\/warp\/v1\/auth\/token$/ });
  await page.goto("/ui/");
  await expect(page.getByTestId("server-version")).toContainText("Hardcharger");
  await page.getByLabel("User name").fill(harness.admin.username);
  await page.getByLabel("Password").fill(`${harness.admin.password}-wrong`);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByTestId("signin-error")).toHaveText("the user name or password is not correct");
  await expect(page.getByLabel("Password")).toHaveValue("");
  await expect(page.getByTestId("header-user")).toHaveCount(0);
  // Positive twin: the right password signs in.
  await page.getByLabel("Password").fill(harness.admin.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByTestId("header-user")).toContainText("admin");
});

test("signing in shows who we are; signing out forgets the token", async ({ page, harness }) => {
  await signIn(page, harness.admin);
  await expect(page.getByTestId("header-user")).toContainText("admin");
  await expect(page.getByTestId("header-version")).toContainText(/^v?\d+\.\d+/);
  const saved = await page.evaluate(() => sessionStorage.getItem("hardcharger-ui:sign-in"));
  expect(saved).toContain("token");
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.getItem("hardcharger-ui:sign-in"))).toBeNull();
});

test("a returning visitor's reload keeps the session and the tabs, with no errors", async ({
  page,
  context,
  harness,
}) => {
  await signIn(page, harness.admin);
  await runSql(page, "SELECT 'still here' AS greeting;");
  expect((await gridRows(page))[1]).toEqual(["still here"]);
  const token = JSON.parse((await page.evaluate(() => sessionStorage.getItem("hardcharger-ui:sign-in")))!)
    .token as string;

  await page.reload();
  await expect(page.getByTestId("header-user")).toContainText("admin");
  await expect(page.locator(".editor-tab:not([hidden]) .view-lines")).toContainText("still here");
  await page.keyboard.press("Escape");
  await page.locator(".editor-tab:not([hidden]) .monaco-editor").click();
  await page.keyboard.press("ControlOrMeta+Enter");
  expect((await gridRows(page))[1]).toEqual(["still here"]);

  // The token lives in this tab's sessionStorage only: never in localStorage or a cookie.
  const local = await page.evaluate(() =>
    JSON.stringify(Object.keys(localStorage).map((k) => [k, localStorage.getItem(k)])),
  );
  expect(local).not.toContain(token);
  expect(await context.cookies()).toEqual([]);
});
