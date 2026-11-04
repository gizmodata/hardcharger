// Other Hardcharger servers in the schema browser, through the relay on the UI's own server:
// adding one (plain HTTP, TLS verified against the server's trusted roots, TLS trusted by a
// fingerprint the person accepts), the lock beside each, signing in, its catalogs in the
// tree, a tab bound to it, a preview, the search across servers, disconnecting; the list
// surviving a reload and the sign-in staying in this tab only.
import type { Locator, Page } from "@playwright/test";

import type { RemoteServer } from "./harness.ts";
import {
  activeTab,
  editorFocused,
  editorTabs,
  expect,
  expectProblem,
  gridRows,
  runDone,
  setSql,
  signIn,
  test,
} from "./fixtures.ts";

function tree(page: Page) {
  return page.getByRole("tree", { name: "Servers" });
}

function serverItem(page: Page, name: string): Locator {
  return tree(page).getByRole("treeitem", { name, exact: true });
}

/** Adds `remote` as `name` and signs in; `trust` accepts a self-signed certificate on the way. */
async function addServer(page: Page, name: string, remote: RemoteServer, trust = false): Promise<Locator> {
  await page.getByTestId("explorer-add-server").click();
  const dialog = page.getByTestId("add-server-dialog");
  await dialog.getByLabel("Name").fill(name);
  await dialog.getByLabel("Server URL").fill(remote.url);
  await dialog.getByTestId("add-server-next").click();
  if (trust) {
    await expect(dialog.getByTestId("certificate-fingerprint")).toHaveText(remote.fingerprint!);
    await dialog.getByTestId("trust-certificate").click();
  }
  await dialog.getByLabel("User name").fill(remote.admin.username);
  await dialog.getByLabel("Password").fill(remote.admin.password);
  await dialog.getByTestId("add-server-sign-in").click();
  await expect(dialog).toHaveCount(0);
  const item = serverItem(page, name);
  await expect(item).toHaveAttribute("data-connected", "true");
  return item;
}

test.beforeEach(async ({ page, harness }) => {
  await signIn(page, harness.admin);
  await editorFocused(page);
});

test("this server is the first node, with the page's own lock, and its catalogs beneath", async ({ page, harness }) => {
  const local = serverItem(page, "This server");
  await expect(local).toHaveAttribute("aria-expanded", "true");
  await expect(local.locator(".tls-lock").first()).toHaveAttribute("data-tls", "plaintext"); // the e2e origin is http
  await expect(local.getByRole("treeitem", { name: harness.catalog, exact: true })).toBeVisible();
  await expect(page.getByTestId("explorer-no-servers")).toHaveCount(0);
});

test("a plain server: a red open lock, sign-in, its catalogs, a tab on it, a preview and the search", async ({
  page,
  harness,
}) => {
  const plain = await addServer(page, "Plain", harness.remotes.plain);
  await expect(plain.locator(".tls-lock").first()).toHaveAttribute("data-tls", "plaintext");
  await expect(plain.getByRole("treeitem", { name: "e2e", exact: true })).toBeVisible();
  // Its own tree: the remote table, not this server's.
  const memory = plain.getByRole("treeitem", { name: "e2e", exact: true });
  await memory.getByRole("button", { name: "e2e", exact: true }).click();
  const main = memory.getByRole("treeitem", { name: "main", exact: true });
  await main.getByRole("button", { name: "main", exact: true }).click();
  await expect(main.getByRole("treeitem", { name: "remote", exact: true })).toBeVisible();
  await expect(main.getByRole("treeitem", { name: "orders", exact: true })).toHaveCount(0);

  // A preview opens a tab bound to the server and runs there.
  const row = main.getByRole("treeitem", { name: "remote", exact: true }).locator(":scope > .tree-row-wrap");
  await row.hover();
  await row.getByRole("button", { name: /^More for/ }).click();
  await page.getByRole("menuitem", { name: "Preview data" }).click();
  await expect(activeTab(page).getByTestId("tab-server")).toHaveText("Plain");
  await expect.poll(async () => (await gridRows(page)).length).toBe(4);
  expect((await gridRows(page))[1]).toEqual(["0", "remote row 0"]);
  // The tab strip names the server on the tab.
  await expect(editorTabs(page).locator(".tab-server").first()).toHaveText("Plain");

  // SQL typed into the tab runs on the remote: its user, and its tables.
  await runDone(page, "SELECT count(*) AS n FROM remote");
  expect((await gridRows(page))[1]).toEqual(["3"]);

  // New tab on… offers both servers.
  await page.getByTestId("new-tab-on").click();
  await expect(page.getByRole("menuitem", { name: "This server" })).toBeVisible();
  await page.getByRole("menuitem", { name: "Plain" }).click();
  await expect(activeTab(page).getByTestId("tab-server")).toHaveText("Plain");

  // The search spans every connected server, grouped by server.
  await page.getByLabel("Search tables and views").fill("remote");
  const results = page.getByTestId("search-results");
  await expect(results.locator(".search-group-head", { hasText: "Plain" })).toBeVisible();
  await expect(results.getByRole("button", { name: /remote/ })).toHaveCount(1);
  await page.getByLabel("Clear the search").click();

  // Disconnect: the node stays, signed out, and asks to sign in when opened.
  const serverRow = plain.locator(":scope > .tree-row-wrap");
  await serverRow.hover();
  await serverRow.getByRole("button", { name: "More for Plain" }).click();
  await page.getByRole("menuitem", { name: "Disconnect" }).click();
  await expect(plain).toHaveAttribute("data-connected", "false");
  await expect(plain).toContainText("not signed in");
});

test("a TLS server the UI's server trusts: a green lock; one it doesn't: trust by fingerprint, an amber lock", async ({
  page,
  harness,
}) => {
  const signed = await addServer(page, "Signed", harness.remotes.signed);
  await expect(signed.locator(".tls-lock").first()).toHaveAttribute("data-tls", "verified");
  // The self-signed server's first probe is refused (502 remote_certificate_untrusted): that
  // is what brings up the certificate to trust.
  expectProblem(page, { status: 502, url: /\/ui\/v1\/remotes\/probe$/ });
  const trusted = await addServer(page, "Self-signed", harness.remotes.selfSigned, true);
  await expect(trusted.locator(".tls-lock").first()).toHaveAttribute("data-tls", "fingerprint");
  // Both answer.
  for (const name of ["Signed", "Self-signed"]) {
    await page.getByTestId("new-tab-on").click();
    await page.getByRole("menuitem", { name, exact: true }).click();
    await runDone(page, "SELECT max(id) AS m FROM remote");
    expect((await gridRows(page))[1]).toEqual(["2"]);
  }
});

test("a wrong password is the server's error; the list survives a reload, the sign-in does not reach a new tab", async ({
  page,
  harness,
  context,
}) => {
  // The wrong password is the remote's 401, through the relay.
  expectProblem(page, { status: 401, url: /\/chargewire\/v1\/auth\/token$/ });
  await page.getByTestId("explorer-add-server").click();
  const dialog = page.getByTestId("add-server-dialog");
  await dialog.getByLabel("Name").fill("Plain");
  await dialog.getByLabel("Server URL").fill(harness.remotes.plain.url);
  await dialog.getByTestId("add-server-next").click();
  await dialog.getByLabel("User name").fill("admin");
  await dialog.getByLabel("Password").fill("wrong");
  await dialog.getByTestId("add-server-sign-in").click();
  await expect(dialog.getByTestId("add-server-error")).toContainText(/credentials|password|refused/i);
  await dialog.getByLabel("Password").fill(harness.remotes.plain.admin.password);
  await dialog.getByTestId("add-server-sign-in").click();
  await expect(dialog).toHaveCount(0);
  await expect(serverItem(page, "Plain")).toHaveAttribute("data-connected", "true");

  // A reload: the server is still listed and still signed in (this tab's sessionStorage).
  await page.reload();
  await editorFocused(page);
  await expect(serverItem(page, "Plain")).toHaveAttribute("data-connected", "true");
  await page.getByTestId("new-tab-on").click();
  await page.getByRole("menuitem", { name: "Plain" }).click();
  await runDone(page, "SELECT 1 AS one FROM remote LIMIT 1");
  expect((await gridRows(page))[1]).toEqual(["1"]);

  // A new browser tab: the server is listed (localStorage), but not signed in.
  const other = await context.newPage();
  await signIn(other, harness.admin);
  await editorFocused(other);
  await expect(serverItem(other, "Plain")).toHaveAttribute("data-connected", "false");
  await expect(other.getByTestId("explorer-no-servers")).toHaveCount(0);
  // Its sessionStorage holds no token for the remote.
  const stored = await other.evaluate(() => sessionStorage.getItem("hardcharger-ui:remote-sign-ins:v1"));
  expect(stored ?? "[]").not.toContain(harness.remotes.plain.url);
  await other.close();

  // Removing it from the list.
  const row = serverItem(page, "Plain").locator(":scope > .tree-row-wrap");
  await row.hover();
  await row.getByRole("button", { name: "More for Plain" }).click();
  page.once("dialog", (d) => void d.accept());
  await page.getByRole("menuitem", { name: "Remove from the list" }).click();
  await expect(serverItem(page, "Plain")).toHaveCount(0);
  await expect(serverItem(page, "This server")).toBeVisible();
  await setSql(page, "");
});
