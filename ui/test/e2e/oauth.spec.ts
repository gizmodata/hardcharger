// Browser sign-in (Chargewire §4.3) from the sign-in page, on a server of its own configured
// for a mock identity provider (idp.ts) that approves at once. The page goes to the
// provider, comes back with the flow id in the fragment, collects the token with the poll
// secret it kept in this tab, and is signed in; nothing of it stays in the URL.
import { type Idp, startIdp } from "./idp.ts";
import { type OAuthServer, startOAuthServer } from "./harness.ts";
import { expect, test } from "./fixtures.ts";

let idp: Idp;
let server: OAuthServer;

test.beforeAll(async () => {
  idp = await startIdp();
  server = await startOAuthServer(idp);
});
test.afterAll(async () => {
  await server?.stop();
  await idp?.stop();
});
test.beforeEach(() => {
  idp.email = "alice@example.com";
  idp.deny = false;
  idp.authorizations = 0;
});

test("Sign in with the provider comes back signed in, with nothing left in the URL", async ({ page }) => {
  await page.goto(`${server.url}/ui/`);
  const button = page.getByTestId("signin-browser");
  await expect(button).toHaveText("Sign in with Mock IdP");
  // The password form stays beside it.
  await expect(page.getByLabel("User name")).toBeVisible();
  await button.click();
  await expect(page.getByTestId("header-user")).toContainText("alice@example.com");
  expect(new URL(page.url()).hash).toBe("");
  expect(page.url()).not.toContain("chargewire_oauth_flow");
  expect(await page.evaluate(() => sessionStorage.getItem("hardcharger-ui:browser-sign-in"))).toBeNull();
  // Only the access token is kept, in this tab.
  expect(await page.evaluate(() => sessionStorage.getItem("hardcharger-ui:sign-in"))).toContain("token");
  // Signing out and in again starts a new sign-in (the old flow is not reused).
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByTestId("signin-browser")).toBeVisible();
  await expect(page.getByTestId("header-user")).toHaveCount(0);
  idp.email = "bob@example.com";
  await page.getByTestId("signin-browser").click();
  await expect(page.getByTestId("header-user")).toContainText("bob@example.com");
});

test("a refusal at the provider brings the page back with the reason", async ({ page }) => {
  idp.deny = true;
  await page.goto(`${server.url}/ui/`);
  await page.getByTestId("signin-browser").click();
  await expect(page.getByTestId("signin-error")).toHaveText("Sign-in failed: access_denied: The user declined");
  await expect(page.getByTestId("header-user")).toHaveCount(0);
  // Positive twin: the next attempt, approved, signs in.
  idp.deny = false;
  await page.getByTestId("signin-browser").click();
  await expect(page.getByTestId("header-user")).toContainText("alice@example.com");
});

test("a user the server doesn't authorize is refused, saying who", async ({ page }) => {
  idp.email = "mallory@elsewhere.example";
  await page.goto(`${server.url}/ui/`);
  await page.getByTestId("signin-browser").click();
  await expect(page.getByTestId("signin-error")).toContainText("mallory@elsewhere.example");
  await expect(page.getByTestId("header-user")).toHaveCount(0);
});

test("a link with someone else's flow id is ignored", async ({ page }) => {
  await page.goto(`${server.url}/ui/#chargewire_oauth_flow=f_not_started_here`);
  await expect(page.getByTestId("signin-browser")).toBeEnabled();
  await expect(page.getByTestId("signin-error")).toHaveCount(0);
  expect(new URL(page.url()).hash).toBe("");
  await expect(page.getByTestId("header-user")).toHaveCount(0);
});

// A link that asks for it (?sign_in=browser, as GizmoData Cloud's Connect opens) goes to the
// provider without a click, once: the parameter leaves the address bar before the flow starts.
test("a link with ?sign_in=browser goes straight to the provider and comes back signed in", async ({ page }) => {
  await page.goto(`${server.url}/ui/?sign_in=browser`);
  await expect(page.getByTestId("header-user")).toContainText("alice@example.com");
  expect(idp.authorizations).toBe(1);
  const url = new URL(page.url());
  expect(url.pathname + url.search + url.hash).toBe("/ui/");
  // A reload stays signed in and doesn't go to the provider again.
  await page.reload();
  await expect(page.getByTestId("header-user")).toContainText("alice@example.com");
  // Signed out, the sign-in page waits for a click.
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByTestId("signin-browser")).toBeEnabled();
  expect(idp.authorizations).toBe(1);
});

test("a refusal after a ?sign_in=browser link shows the reason and doesn't loop", async ({ page }) => {
  idp.deny = true;
  await page.goto(`${server.url}/ui/?sign_in=browser`);
  await expect(page.getByTestId("signin-error")).toHaveText("Sign-in failed: access_denied: The user declined");
  await expect(page.getByTestId("signin-browser")).toBeEnabled();
  expect(new URL(page.url()).search).toBe("");
  await page.reload();
  // Discovery has answered (the page knows the server), and nothing started.
  await expect(page.getByTestId("server-version")).toContainText("Hardcharger");
  await expect(page.getByTestId("signin-browser")).toBeEnabled();
  await expect(page.getByTestId("signin-error")).toHaveCount(0);
  expect(idp.authorizations).toBe(1);
  // Positive twin: the button still works.
  idp.deny = false;
  await page.getByTestId("signin-browser").click();
  await expect(page.getByTestId("header-user")).toContainText("alice@example.com");
  expect(idp.authorizations).toBe(2);
});
