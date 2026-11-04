// Other Hardcharger servers in the schema browser, through the relay on the UI's own server,
// added with the one-page Add a server dialog: host and port (or a pasted hardcharger:// URI),
// a user name and password (or a scoped token as user `token`'s password), Use TLS and Trust
// this server's certificate (the certificate shown inline, then pinned), each lock's colour,
// the query timeout, saved connections filling the form, the saved list surviving a reload
// without any secret, and the sign-in staying in this tab only.
import { connect } from "@gizmodata/chargewire";
import type { Locator, Page } from "@playwright/test";

import type { RemoteServer } from "./harness.ts";
import {
  activeTab,
  answerDialog,
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

function dialogOf(page: Page) {
  return page.getByTestId("add-server-dialog");
}

/** host and port of a harness remote's URL. */
function hostPort(remote: RemoteServer): { host: string; port: string; tls: boolean } {
  const u = new URL(remote.url);
  return { host: u.hostname, port: u.port, tls: u.protocol === "https:" };
}

interface AddOptions {
  /** Paste this into Host instead of typing host and port. */
  uri?: string;
  trust?: boolean;
  username?: string;
  password?: string;
  timeout?: number;
  save?: boolean;
}

/** Fills the dialog for `remote` and connects; with trust, confirms the certificate shown. */
async function addServer(page: Page, name: string, remote: RemoteServer, o: AddOptions = {}): Promise<Locator> {
  await page.getByTestId("explorer-add-server").click();
  const dialog = dialogOf(page);
  await dialog.getByLabel("Display name", { exact: true }).fill(name);
  if (o.uri) {
    await dialog.getByLabel("Host", { exact: true }).fill(o.uri);
  } else {
    const a = hostPort(remote);
    await dialog.getByLabel("Host", { exact: true }).fill(a.host);
    await dialog.getByLabel("Port", { exact: true }).fill(a.port);
    if (!a.tls) await dialog.getByLabel("Use TLS", { exact: true }).uncheck();
  }
  if (o.trust) await dialog.getByLabel("Trust this server's certificate").check();
  await dialog.getByLabel("Username", { exact: true }).fill(o.username ?? remote.admin.username);
  await dialog.getByLabel("Password", { exact: true }).fill(o.password ?? remote.admin.password);
  if (o.timeout !== undefined) await dialog.getByLabel("Query timeout (seconds)").fill(String(o.timeout));
  if (o.save === false) await dialog.getByLabel("Save connection for later").uncheck();
  await dialog.getByTestId("add-server-connect").click();
  if (o.trust) {
    await expect(dialog.getByTestId("certificate-fingerprint")).toHaveText(remote.fingerprint!);
    await expect(dialog.getByTestId("add-server-connect")).toHaveText(/Trust and connect/);
    await dialog.getByTestId("add-server-connect").click();
  }
  await expect(dialog).toHaveCount(0);
  const item = serverItem(page, name);
  await expect(item).toHaveAttribute("data-connected", "true");
  return item;
}

test.beforeEach(async ({ page, harness }) => {
  await signIn(page, harness.admin);
  await editorFocused(page);
});

test("the dialog is one page: every field visible, Port 31337, Use TLS on, SSO off with its reason", async ({
  page,
}) => {
  await page.getByTestId("explorer-add-server").click();
  const dialog = dialogOf(page);
  for (const label of [
    "Display name",
    "Host",
    "Port",
    "Username",
    "Password",
    "Use TLS",
    "Trust this server's certificate",
    "Query timeout (seconds)",
    "Save connection for later",
    "Remember password",
  ]) {
    await expect(dialog.getByLabel(label, { exact: true })).toBeVisible();
  }
  await expect(dialog.getByLabel("Port", { exact: true })).toHaveValue("31337");
  await expect(dialog.getByLabel("Use TLS", { exact: true })).toBeChecked();
  await expect(dialog.getByLabel("Trust this server's certificate")).not.toBeChecked();
  await expect(dialog.getByLabel("Save connection for later")).toBeChecked();
  await expect(dialog.getByLabel("Query timeout (seconds)")).toHaveValue("0");
  await expect(dialog.getByText("0 = unlimited (no timeout)")).toBeVisible();
  await expect(dialog.getByRole("radio", { name: "Username & Password" })).toBeChecked();
  await expect(dialog.getByRole("radio", { name: "SSO / OAuth" })).toBeDisabled();
  await expect(dialog.getByText(/SSO\) to another server isn't supported yet/)).toBeVisible();
  await expect(
    dialog.getByText("For a scoped token, use the user name token and the token as the password."),
  ).toBeVisible();
  // In a browser there is nowhere safe to keep a password: Remember password is the desktop app's.
  await expect(dialog.getByLabel("Remember password")).toBeDisabled();
  await expect(dialog.locator("label", { hasText: "Remember password" })).toHaveAttribute(
    "title",
    "Available in the Hardcharger desktop app",
  );
  // Unchecking Use TLS turns Trust off and the lock red.
  await dialog.getByLabel("Use TLS", { exact: true }).uncheck();
  await expect(dialog.getByLabel("Trust this server's certificate")).toBeDisabled();
  await expect(dialog.locator(".server-checks .tls-lock")).toHaveAttribute("data-tls", "plaintext");
  // Connect with nothing filled in says what is missing, under its field.
  await dialog.getByTestId("add-server-connect").click();
  await expect(dialog.getByTestId("add-server-error-host")).toContainText("host");
});

test("this server is the first node, with the page's own lock, and its catalogs beneath", async ({ page, harness }) => {
  const local = serverItem(page, "This server");
  await expect(local).toHaveAttribute("aria-expanded", "true");
  await expect(local.locator(".tls-lock").first()).toHaveAttribute("data-tls", "plaintext"); // the e2e origin is http
  await expect(local.getByRole("treeitem", { name: harness.catalog, exact: true })).toBeVisible();
  await expect(page.getByTestId("explorer-no-servers")).toHaveCount(0);
});

test("a pasted hardcharger:// URI fills host, port, TLS and the user name", async ({ page, harness }) => {
  const a = hostPort(harness.remotes.plain);
  await page.getByTestId("explorer-add-server").click();
  const dialog = dialogOf(page);
  await dialog
    .getByLabel("Host", { exact: true })
    .fill(`hardcharger://ann:not-kept@${a.host}:${a.port}/proxy/cw?tls=false`);
  await expect(dialog.getByLabel("Host", { exact: true })).toHaveValue(a.host);
  await expect(dialog.getByLabel("Port", { exact: true })).toHaveValue(a.port);
  await expect(dialog.getByLabel("Use TLS", { exact: true })).not.toBeChecked();
  await expect(dialog.getByLabel("Username", { exact: true })).toHaveValue("ann");
  await expect(dialog.getByLabel("Password", { exact: true })).toHaveValue("");
  await dialog.getByText("Advanced").click();
  await expect(dialog.getByLabel("Path prefix")).toHaveValue("/proxy/cw");
  // Junk is refused under Host, with the reason.
  await dialog.getByLabel("Host", { exact: true }).fill("ftp://example.com");
  await expect(dialog.getByTestId("add-server-error-host")).toContainText("ftp://");
});

test("a plain server: a red open lock, sign-in, its catalogs, a tab on it, a preview and the search", async ({
  page,
  harness,
}) => {
  const a = hostPort(harness.remotes.plain);
  const plain = await addServer(page, "Plain", harness.remotes.plain, {
    uri: `hardcharger://${a.host}:${a.port}?tls=false`,
  });
  await expect(plain.locator(".tls-lock").first()).toHaveAttribute("data-tls", "plaintext");
  const catalog = plain.getByRole("treeitem", { name: "e2e", exact: true });
  await expect(catalog).toBeVisible();
  await catalog.getByRole("button", { name: "e2e", exact: true }).click();
  const main = catalog.getByRole("treeitem", { name: "main", exact: true });
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
  await expect(editorTabs(page).locator(".tab-server").first()).toHaveText("Plain");

  await runDone(page, "SELECT count(*) AS n FROM remote");
  expect((await gridRows(page))[1]).toEqual(["3"]);

  await page.getByTestId("new-tab-on").click();
  await expect(page.getByRole("menuitem", { name: "This server" })).toBeVisible();
  await page.getByRole("menuitem", { name: "Plain", exact: true }).click();
  await expect(activeTab(page).getByTestId("tab-server")).toHaveText("Plain");

  await page.getByLabel("Search tables and views").fill("remote");
  const results = page.getByTestId("search-results");
  await expect(results.locator(".search-group-head", { hasText: "Plain" })).toBeVisible();
  await expect(results.getByRole("button", { name: /remote/ })).toHaveCount(1);
  await page.getByLabel("Clear the search").click();

  const serverRow = plain.locator(":scope > .tree-row-wrap");
  await serverRow.hover();
  await serverRow.getByRole("button", { name: "More for Plain" }).click();
  await page.getByRole("menuitem", { name: "Disconnect" }).click();
  await expect(plain).toHaveAttribute("data-connected", "false");
  await expect(plain).toContainText("not signed in");
});

test("TLS: green when the certificate verifies; a private one fails with a hint, then is trusted and pinned (amber)", async ({
  page,
  harness,
}) => {
  const signed = await addServer(page, "Signed", harness.remotes.signed);
  await expect(signed.locator(".tls-lock").first()).toHaveAttribute("data-tls", "verified");

  // Without Trust, the self-signed server's certificate fails, and the error says what to do.
  expectProblem(page, { status: 502, url: /\/ui\/v1\/remotes\/probe$/ });
  const self = hostPort(harness.remotes.selfSigned);
  await page.getByTestId("explorer-add-server").click();
  const dialog = dialogOf(page);
  await dialog.getByLabel("Host", { exact: true }).fill(self.host);
  await dialog.getByLabel("Port", { exact: true }).fill(self.port);
  await dialog.getByLabel("Username", { exact: true }).fill(harness.remotes.selfSigned.admin.username);
  await dialog.getByLabel("Password", { exact: true }).fill(harness.remotes.selfSigned.admin.password);
  await dialog.getByTestId("add-server-connect").click();
  await expect(dialog.getByTestId("add-server-error-tls")).toContainText("Trust this server's certificate");
  await dialog.getByRole("button", { name: "Cancel" }).click();

  const trusted = await addServer(page, "Self-signed", harness.remotes.selfSigned, { trust: true });
  await expect(trusted.locator(".tls-lock").first()).toHaveAttribute("data-tls", "fingerprint");
  for (const name of ["Signed", "Self-signed"]) {
    await page.getByTestId("new-tab-on").click();
    await page.getByRole("menuitem", { name, exact: true }).click();
    await runDone(page, "SELECT max(id) AS m FROM remote");
    expect((await gridRows(page))[1]).toEqual(["2"]);
  }
  // The pinned fingerprint is saved with the connection (no secret beside it).
  const stored = await page.evaluate(() =>
    Object.keys(localStorage)
      .filter((k) => k.startsWith("hardcharger-ui:servers:v1:"))
      .map((k) => localStorage.getItem(k))
      .join(""),
  );
  expect(stored).toContain(harness.remotes.selfSigned.fingerprint!);
  expect(stored).not.toContain(harness.remotes.selfSigned.admin.password);
});

test("a scoped token signs in as user token, and the query timeout cancels a long statement", async ({
  page,
  harness,
}) => {
  const remote = harness.remotes.plain;
  const admin = await connect({ url: remote.url, ...remote.admin });
  let token: string;
  try {
    // A user who may read the table, and a scoped token of theirs (the administrator names the user).
    const suffix = Math.random().toString(36).slice(2, 8);
    for (const sql of [
      "CREATE USER IF NOT EXISTS ui_token_user PASSWORD 'pw:ui-token-1'",
      "CREATE ROLE IF NOT EXISTS ui_token_readers",
      "GRANT SELECT ON remote TO ui_token_readers",
      "GRANT ui_token_readers TO ui_token_user",
    ]) {
      await admin.execute(sql);
    }
    const row = (await admin.query(`CREATE SCOPED TOKEN ui_token_${suffix} FOR USER ui_token_user ALLOW SELECT`))
      .get(0)!
      .toJSON() as { token: string };
    token = row.token;
  } finally {
    await admin.close();
  }
  // Grants reach the remote's sessions within about a second (it polls its store).
  await page.waitForTimeout(1500);
  await addServer(page, "By token", remote, { username: "token", password: token, timeout: 1 });
  await page.getByTestId("new-tab-on").click();
  await page.getByRole("menuitem", { name: "By token", exact: true }).click();
  await runDone(page, "SELECT count(*) AS n FROM remote");
  expect((await gridRows(page))[1]).toEqual(["3"]);
  // The saved connection's 1 s timeout: the server cancels the statement.
  // The server cuts the running result (spec §6.6): browsers log that as a failed load.
  expectProblem(page, { status: 408, required: false });
  expectProblem(page, {
    console:
      /ERR_INCOMPLETE_CHUNKED_ENCODING|Failed to load resource: (The network connection was lost|Connection terminated unexpectedly)/,
    required: false,
  });
  await runDone(page, "SELECT count(*) FROM range(100000000000) a(i) WHERE i % 7 = 3");
  await expect(page.getByTestId("query-error")).toContainText(/time limit|timeout|timed out/i);
});

test("a wrong password shows under Password; saved connections fill the form; the list survives a reload without secrets", async ({
  page,
  harness,
  context,
}) => {
  expectProblem(page, { status: 401, url: /\/chargewire\/v1\/auth\/token$/ });
  const remote = harness.remotes.plain;
  const a = hostPort(remote);
  await page.getByTestId("explorer-add-server").click();
  const dialog = dialogOf(page);
  await dialog.getByLabel("Display name", { exact: true }).fill("Plain");
  await dialog.getByLabel("Host", { exact: true }).fill(a.host);
  await dialog.getByLabel("Port", { exact: true }).fill(a.port);
  await dialog.getByLabel("Use TLS", { exact: true }).uncheck();
  await dialog.getByLabel("Username", { exact: true }).fill("admin");
  await dialog.getByLabel("Password", { exact: true }).fill("wrong");
  await dialog.getByLabel("Query timeout (seconds)").fill("45");
  await dialog.getByTestId("add-server-connect").click();
  await expect(dialog.getByTestId("add-server-error-password")).toContainText(/credentials|password|refused/i);
  await dialog.getByLabel("Password", { exact: true }).fill(remote.admin.password);
  await dialog.getByTestId("add-server-connect").click();
  await expect(dialog).toHaveCount(0);
  await expect(serverItem(page, "Plain")).toHaveAttribute("data-connected", "true");

  // A reload: still listed and signed in (this tab's sessionStorage); no secret in localStorage.
  await page.reload();
  await editorFocused(page);
  await expect(serverItem(page, "Plain")).toHaveAttribute("data-connected", "true");
  const local = await page.evaluate(() =>
    JSON.stringify(Object.fromEntries(Object.keys(localStorage).map((k) => [k, localStorage.getItem(k)]))),
  );
  expect(local).not.toContain(remote.admin.password);
  expect(local).toContain('\\"timeout\\":45');

  // Saved connections fill the form: the rows at the top, and the chooser in Display name.
  await page.getByTestId("explorer-add-server").click();
  const again = dialogOf(page);
  await again.getByTestId("saved-connections").getByRole("button", { name: /Plain/ }).first().click();
  await expect(again.getByLabel("Display name", { exact: true })).toHaveValue("Plain");
  await expect(again.getByLabel("Host", { exact: true })).toHaveValue(a.host);
  await expect(again.getByLabel("Port", { exact: true })).toHaveValue(a.port);
  await expect(again.getByLabel("Use TLS", { exact: true })).not.toBeChecked();
  await expect(again.getByLabel("Username", { exact: true })).toHaveValue("admin");
  await expect(again.getByLabel("Password", { exact: true })).toHaveValue("");
  await expect(again.getByLabel("Query timeout (seconds)")).toHaveValue("45");
  await again.getByLabel("Display name", { exact: true }).fill("");
  await again.getByTestId("saved-connection-chooser").click();
  await again.getByRole("menuitem", { name: /Plain/ }).click();
  await expect(again.getByLabel("Display name", { exact: true })).toHaveValue("Plain");
  await again.getByRole("button", { name: "Cancel" }).click();

  // A new browser tab: listed (localStorage), not signed in, no token for it.
  const other = await context.newPage();
  await signIn(other, harness.admin);
  await editorFocused(other);
  await expect(serverItem(other, "Plain")).toHaveAttribute("data-connected", "false");
  const stored = await other.evaluate(() => sessionStorage.getItem("hardcharger-ui:remote-sign-ins:v1"));
  expect(stored ?? "[]").toBe("[]");
  await other.close();

  // Forgetting it in the dialog removes it from the list.
  await page.getByTestId("explorer-add-server").click();
  await dialogOf(page).getByRole("button", { name: "Forget Plain" }).click();
  await expect(dialogOf(page).getByTestId("saved-connections")).toHaveCount(0);
  await dialogOf(page).getByRole("button", { name: "Cancel" }).click();
  await expect(serverItem(page, "Plain")).toHaveCount(0);
  await expect(serverItem(page, "This server")).toBeVisible();
  await setSql(page, "");
});

test("a connection not saved for later is this tab's only", async ({ page, harness }) => {
  await addServer(page, "Once", harness.remotes.plain, { save: false });
  const local = await page.evaluate(() =>
    JSON.stringify(Object.fromEntries(Object.keys(localStorage).map((k) => [k, localStorage.getItem(k)]))),
  );
  expect(local).not.toContain("Once");
  await page.reload();
  await editorFocused(page);
  await expect(serverItem(page, "Once")).toHaveAttribute("data-connected", "true");
});

async function serverMenu(page: Page, name: string, item: string): Promise<void> {
  const row = serverItem(page, name).locator(":scope > .tree-row-wrap");
  await row.hover();
  await row.getByRole("button", { name: `More for ${name}` }).click();
  await page.getByRole("menuitem", { name: item }).click();
}

test("Rename… and Remove from the list ask in the page (the desktop webview has no prompt or confirm)", async ({
  page,
  harness,
}) => {
  await addServer(page, "Plain", harness.remotes.plain);
  await serverMenu(page, "Plain", "Rename…");
  await answerDialog(page, { text: "Name for this server", value: "Renamed" });
  await expect(serverItem(page, "Renamed")).toBeVisible();
  await expect(serverItem(page, "Plain")).toHaveCount(0);
  // Cancel keeps it; Remove removes it, and a reload doesn't bring it back.
  await serverMenu(page, "Renamed", "Remove from the list");
  await answerDialog(page, { accept: false, text: "Remove Renamed from the list?" });
  await expect(serverItem(page, "Renamed")).toBeVisible();
  await serverMenu(page, "Renamed", "Remove from the list");
  await answerDialog(page, { text: "Its tabs stay open but can't run." });
  await expect(serverItem(page, "Renamed")).toHaveCount(0);
  await page.reload();
  await editorFocused(page);
  await expect(serverItem(page, "Renamed")).toHaveCount(0);
});

test("a server the desktop app handed over stays removed, until it is added again", async ({ page, harness }) => {
  const a = hostPort(harness.remotes.plain);
  // What the desktop app's workspace gets at launch (its Settings > Remote servers), kept in
  // the tab's sessionStorage, so every load merges it in again.
  await page.addInitScript(
    (server) => sessionStorage.setItem("hardcharger-ui:desktop-servers", JSON.stringify([server])),
    { id: "desk-1", name: "From desktop", host: a.host, port: Number(a.port), tls: false, prefix: "", trust: false },
  );
  await page.reload();
  await editorFocused(page);
  await expect(serverItem(page, "From desktop")).toHaveAttribute("data-connected", "false");
  await serverMenu(page, "From desktop", "Remove from the list");
  await answerDialog(page);
  await expect(serverItem(page, "From desktop")).toHaveCount(0);
  await page.reload();
  await editorFocused(page);
  await expect(serverItem(page, "From desktop")).toHaveCount(0);
  // Adding it again brings it back, and it stays at the next load.
  await addServer(page, "Again", harness.remotes.plain);
  await page.reload();
  await editorFocused(page);
  await expect(serverItem(page, "Again")).toBeVisible();
  await expect(serverItem(page, "From desktop")).toHaveCount(0);
});
