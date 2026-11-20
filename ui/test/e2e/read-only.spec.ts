import { copyFileSync } from "node:fs";
import path from "node:path";

import type { Locator, Page, TestInfo } from "@playwright/test";

import { activeTab, expect, expectProblem, gridRows, runDone, signIn, test, openRowMenu } from "./fixtures.ts";

test("a tab can become read-only, locks the choice, and a new session permits writes", async ({
  page,
  harness,
}, testInfo) => {
  await signIn(page, harness.admin);
  const tab = activeTab(page);
  const mode = tab.getByRole("checkbox", { name: "Read-only", exact: true });
  await runDone(page, "SELECT 1 AS n");
  await runDone(page, "BEGIN");
  await expect(mode).toBeDisabled();
  await expect(tab.getByTestId("session-bar")).toContainText("Finish the transaction");
  await runDone(page, "ROLLBACK");
  await expect(mode).toBeEnabled();
  const original = await tab.getByTestId("session-chip").textContent();
  // The checkbox is confirmed asynchronously by the server, not optimistically.
  await mode.click();
  await expect(mode).toBeChecked();
  await expect(mode).toBeDisabled();
  await expect(tab.getByTestId("session-bar")).toContainText("Locked for this session");
  await expect(tab.getByTestId("upload-button")).toBeDisabled();
  await runDone(page, "SELECT 2 AS n");
  await expect(tab.getByTestId("query-error")).toHaveCount(0);
  expectProblem(page, { status: 400, url: /\/statements$/, required: false });
  expectProblem(page, { status: 403, url: /\/statements$/, required: false });
  await runDone(page, "CREATE TABLE readonly_refused (i INTEGER)");
  await expect(tab.getByTestId("query-error")).toContainText(/read.only/i);
  await page.screenshot({ path: testInfo.outputPath("read-only-locked.png") });
  await tab.getByRole("button", { name: "New session", exact: true }).click();
  await expect(mode).toBeEnabled();
  await mode.uncheck();
  await runDone(page, "CREATE TEMP TABLE writable_again (i INTEGER)");
  await expect(tab.getByTestId("query-error")).toHaveCount(0);
  await expect(tab.getByTestId("session-chip")).not.toHaveText(original!);
  await expect(mode).not.toBeChecked();
});

test("read-only selected before the first run is sent in session creation", async ({ page, harness }) => {
  await signIn(page, harness.admin);
  const tab = activeTab(page);
  await tab.getByRole("checkbox", { name: "Read-only", exact: true }).check();
  const created = page.waitForRequest(
    (r) => r.method() === "POST" && r.url().endsWith("/sessions") && r.postDataJSON()?.options?.read_only === true,
  );
  await runDone(page, "SELECT 42 AS n");
  await created;
  await expect(tab.getByRole("checkbox", { name: "Read-only", exact: true })).toBeDisabled();
  await expect(tab.getByTestId("query-error")).toHaveCount(0);
});

test("connection preference opens remote tabs read-only and survives reload", async ({ page, harness }, testInfo) => {
  await signIn(page, harness.admin);
  await page.getByTestId("explorer-add-server").click();
  const dialog = page.getByTestId("add-server-dialog");
  await dialog.getByLabel("Display name", { exact: true }).fill("Read-only analytics");
  await dialog.getByLabel("Host", { exact: true }).fill(harness.remotes.plain.url);
  await dialog.getByLabel("Username", { exact: true }).fill(harness.remotes.plain.admin.username);
  await dialog.getByLabel("Password", { exact: true }).fill(harness.remotes.plain.admin.password);
  await dialog.getByRole("checkbox", { name: "Read-only sessions", exact: true }).check();
  await page.screenshot({ path: testInfo.outputPath("read-only-connection.png") });
  await dialog.getByTestId("add-server-connect").click();
  await expect(dialog).toHaveCount(0);
  await activeTab(page).getByLabel("Connection", { exact: true }).selectOption({ label: "Read-only analytics" });
  await runDone(page, "SELECT count(*) AS n FROM remote");
  await expect(activeTab(page).getByRole("checkbox", { name: "Read-only", exact: true })).toBeDisabled();
  await page.reload();
  await runDone(page, "SELECT count(*) AS n FROM remote");
  await expect(activeTab(page).getByRole("checkbox", { name: "Read-only", exact: true })).toBeChecked();
  await expect(activeTab(page).getByRole("checkbox", { name: "Read-only", exact: true })).toBeDisabled();
  await expect(activeTab(page).getByTestId("query-error")).toHaveCount(0);
});

/** The tab's Read-only box, its state (data-state: on, off, pending, next-on, next-off) and its note. */
function readOnlyMode(page: Page) {
  const tab = activeTab(page);
  return {
    box: tab.getByRole("checkbox", { name: "Read-only", exact: true }),
    mode: tab.getByTestId("read-only-mode"),
    notice: tab.getByTestId("tab-notice"),
    chip: tab.getByTestId("session-chip"),
    error: tab.getByTestId("query-error"),
  };
}

/** Copies a screenshot next to the review report when HARDCHARGER_UI_TEST_SCREENSHOTS names a folder. */
async function keepScreenshot(page: Page, testInfo: TestInfo, name: string): Promise<void> {
  const file = testInfo.outputPath(`${testInfo.project.name}-${name}`);
  await page.screenshot({ path: file });
  const keep = process.env["HARDCHARGER_UI_TEST_SCREENSHOTS"];
  if (keep) copyFileSync(file, path.join(keep, `${testInfo.project.name}-${name}`));
}

test("temporary sequences: the server's refusal leaves the session and its temporary objects; after DROP the box locks", async ({
  page,
  harness,
}, testInfo) => {
  await signIn(page, harness.admin);
  const ro = readOnlyMode(page);
  await runDone(page, "CREATE TEMP TABLE scratch AS SELECT 42 AS v");
  await runDone(page, "CREATE TEMP SEQUENCE seq");
  await expect(ro.error).toHaveCount(0);
  const session = await ro.chip.textContent();
  await expect(ro.mode).toHaveAttribute("data-state", "off");

  await ro.box.click();
  await expect(ro.notice).toHaveText(
    "Read-only was not enabled: read_only cannot be set while the session has temporary sequences: drop them first. " +
      "The session is unchanged and still read-write; select Read-only again when that is done.",
  );
  await expect(ro.mode).toHaveAttribute("data-state", "off");
  await expect(ro.box).not.toBeChecked();
  await expect(ro.box).toBeEnabled();
  await expect(ro.chip).toHaveText(session!);
  await keepScreenshot(page, testInfo, "read-only-refused.png");

  // The temporary table is still there, in the same session, and the advice can be followed.
  await runDone(page, "SELECT v FROM scratch");
  expect(await gridRows(page)).toEqual([["v"], ["42"]]);
  await runDone(page, "SELECT nextval('seq') AS n");
  expect(await gridRows(page)).toEqual([["n"], ["1"]]);
  await runDone(page, "DROP SEQUENCE seq");
  await expect(ro.error).toHaveCount(0);
  await expect(ro.chip).toHaveText(session!);

  await ro.box.click();
  await expect(ro.mode).toHaveAttribute("data-state", "on");
  await expect(ro.box).toBeChecked();
  await expect(ro.box).toBeDisabled();
  await expect(ro.notice).toHaveText("Read-only is enabled for this session.");
  await expect(ro.chip).toHaveText(session!);
  await runDone(page, "SELECT v FROM scratch");
  expect(await gridRows(page)).toEqual([["v"], ["42"]]);
  await keepScreenshot(page, testInfo, "read-only-locked.png");
  // And the lock is the server's: a write is refused with its read_only error.
  await runDone(page, "CREATE TEMP TABLE refused (i INTEGER)");
  await expect(ro.error).toHaveText("read onlyCREATE is not allowed in a read-only session");
});

test("an open transaction the tab didn't know about: the refusal keeps it open, and its work commits", async ({
  page,
  harness,
}) => {
  await signIn(page, harness.admin);
  const ro = readOnlyMode(page);
  await runDone(page, "CREATE TEMP TABLE txn_target (i INTEGER)");
  const session = await ro.chip.textContent();
  // The session's reads fail while the transaction starts, so the tab doesn't know one is open
  // and leaves the box usable: the server is the one that refuses.
  const sessionDoc = /\/chargewire\/v1\/sessions\/[^/?]+$/;
  let failed = 0;
  await page.route(sessionDoc, async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    failed++;
    await route.fulfill({
      status: 500,
      contentType: "application/problem+json",
      body: JSON.stringify({ code: "internal_error", status: 500, title: "Injected", detail: "injected read failure" }),
    });
  });
  expectProblem(page, { status: 500, url: sessionDoc });
  await runDone(page, "BEGIN");
  await runDone(page, "INSERT INTO txn_target VALUES (7)");
  await page.unroute(sessionDoc);
  expect(failed).toBeGreaterThan(0);
  await expect(activeTab(page).getByTestId("in-transaction")).toHaveCount(0);
  await expect(ro.box).toBeEnabled();

  await ro.box.click();
  await expect(ro.notice).toHaveText(
    "Read-only was not enabled: read_only cannot be set while a transaction with work in it is open: commit or roll back first. " +
      "The session is unchanged and still read-write; select Read-only again when that is done.",
  );
  // The session, its transaction and its work are all still there.
  await expect(activeTab(page).getByTestId("in-transaction")).toBeVisible();
  await expect(ro.mode).toHaveAttribute("data-state", "off");
  await expect(ro.box).not.toBeChecked();
  await expect(ro.box).toBeDisabled(); // now that the tab knows the transaction is open
  await expect(ro.chip).toHaveText(session!);
  await activeTab(page).getByRole("button", { name: "Commit", exact: true }).click();
  await expect(activeTab(page).getByTestId("in-transaction")).toHaveCount(0);
  await runDone(page, "SELECT i FROM txn_target");
  expect(await gridRows(page)).toEqual([["i"], ["7"]]);

  await ro.box.click();
  await expect(ro.mode).toHaveAttribute("data-state", "on");
  await expect(ro.box).toBeChecked();
  await expect(ro.chip).toHaveText(session!);
});

test("re-adding a connected server with Read-only sessions never shows a writable session as read-only, and a reload shows the new session's state", async ({
  page,
  harness,
}) => {
  const remote = harness.remotes.plain;
  const addServer = async (readOnly: boolean) => {
    await page.getByTestId("explorer-add-server").click();
    const dialog = page.getByTestId("add-server-dialog");
    await dialog.getByLabel("Display name", { exact: true }).fill("Re-added");
    await dialog.getByLabel("Host", { exact: true }).fill(remote.url);
    await dialog.getByLabel("Username", { exact: true }).fill(remote.admin.username);
    await dialog.getByLabel("Password", { exact: true }).fill(remote.admin.password);
    await dialog.getByRole("checkbox", { name: "Read-only sessions", exact: true }).setChecked(readOnly);
    await dialog.getByTestId("add-server-connect").click();
    await expect(dialog).toHaveCount(0);
  };
  /** A write with the box checked is refused by the server: never checked and writable. */
  const neverCheckedAndWritable = async (table: string): Promise<boolean> => {
    const checked = await ro.box.isChecked();
    await runDone(page, `CREATE TEMP TABLE ${table} (i INTEGER)`);
    const refused = (await ro.error.count()) > 0;
    if (refused) await expect(ro.error).toHaveText("read onlyCREATE is not allowed in a read-only session");
    if (checked) expect(refused, `${table}: written with the Read-only box checked`).toBe(true);
    return refused;
  };

  await signIn(page, harness.admin);
  const ro = readOnlyMode(page);
  await addServer(false);
  await activeTab(page).getByLabel("Connection", { exact: true }).selectOption({ label: "Re-added" });
  await runDone(page, "CREATE TEMP TABLE writable_before (i INTEGER)");
  await expect(ro.error).toHaveCount(0);
  await expect(ro.mode).toHaveAttribute("data-state", "off");
  const session = await ro.chip.textContent();

  // The same server again, now with read-only sessions. The box doesn't claim the open session,
  // which the tab last read as read-write.
  await addServer(true);
  await expect(ro.mode).toHaveAttribute("data-state", "off");
  await expect(ro.box).not.toBeChecked();
  await expect(ro.mode).toContainText("This session is read-write. New sessions on this connection start read-only.");
  // Registering the server again ended what the page had opened through the old registration
  // (the relay answers 404 for it): the tab's next run opens a new session, as the connection
  // now asks, and the write is refused there.
  expectProblem(page, {
    status: 404,
    url: /\/ui\/v1\/remotes\/[^/]+\/chargewire\/v1\/sessions\/[^/]+(\/statements)?$/,
  });
  expect(await neverCheckedAndWritable("after_readd")).toBe(true);
  await expect(ro.notice).toContainText(
    "A new session was started: the previous one ended (this page signed in to the server again",
  );
  await expect(ro.chip).not.toHaveText(session!);
  await expect(ro.mode).toHaveAttribute("data-state", "on");
  await expect(ro.box).toBeChecked();
  await expect(ro.box).toBeDisabled();

  // New session: the connection's setting for the next one, checked once the server says so.
  await activeTab(page).getByRole("button", { name: "New session", exact: true }).click();
  await expect(ro.mode).toHaveAttribute("data-state", "next-on");
  await runDone(page, "SELECT count(*) AS n FROM remote");
  await expect(ro.mode).toHaveAttribute("data-state", "on");
  await expect(ro.box).toBeDisabled();
  expect(await neverCheckedAndWritable("refused_new_session")).toBe(true);
  await expect(ro.box).toBeChecked();

  // After a reload: before a run, the choice for the next session; after it, the server's word.
  await page.reload();
  await expect(ro.mode).toHaveAttribute("data-state", "next-on");
  await expect(ro.chip).toHaveCount(0);
  await runDone(page, "SELECT count(*) AS n FROM remote");
  await expect(ro.mode).toHaveAttribute("data-state", "on");
  await expect(ro.box).toBeChecked();
  await expect(ro.box).toBeDisabled();
  expect(await neverCheckedAndWritable("refused_after_reload")).toBe(true);
  await expect(ro.box).toBeChecked();
});

test("a reload shows the new session's real state, read-write as well as read-only", async ({ page, harness }) => {
  await signIn(page, harness.admin);
  const ro = readOnlyMode(page);
  await runDone(page, "SELECT 1 AS n");
  await ro.box.click();
  await expect(ro.mode).toHaveAttribute("data-state", "on");

  await page.reload();
  // The stored choice, for the next session; nothing is open yet.
  await expect(ro.mode).toHaveAttribute("data-state", "next-on");
  await expect(ro.chip).toHaveCount(0);
  const created = page.waitForRequest(
    (r) => r.method() === "POST" && r.url().endsWith("/sessions") && r.postDataJSON()?.options?.read_only === true,
  );
  await runDone(page, "SELECT 2 AS n");
  await created;
  await expect(ro.mode).toHaveAttribute("data-state", "on");
  await runDone(page, "CREATE TEMP TABLE refused (i INTEGER)");
  await expect(ro.error).toHaveText("read onlyCREATE is not allowed in a read-only session");

  // Twin: New session, unticked, reload: read-write, and writes go through.
  await activeTab(page).getByRole("button", { name: "New session", exact: true }).click();
  await ro.box.uncheck();
  await expect(ro.mode).toHaveAttribute("data-state", "next-off");
  await page.reload();
  await expect(ro.mode).toHaveAttribute("data-state", "next-off");
  await runDone(page, "CREATE TEMP TABLE writable (i INTEGER)");
  await expect(ro.error).toHaveCount(0);
  await expect(ro.mode).toHaveAttribute("data-state", "off");
  await expect(ro.box).not.toBeChecked();
});

/** Drags a file over the page; `drop` lets go of it. */
async function dragFile(page: Page, name: string, drop: boolean): Promise<void> {
  const dt = await page.evaluateHandle((file) => {
    const d = new DataTransfer();
    d.items.add(new File(["a,b\n1,2\n"], file, { type: "text/csv" }));
    return d;
  }, name);
  await page.dispatchEvent("main", "dragenter", { dataTransfer: dt });
  if (drop) await page.dispatchEvent("main", "drop", { dataTransfer: dt });
}

test("a file dropped on a read-only tab is not offered an upload, and opens nothing", async ({ page, harness }) => {
  await signIn(page, harness.admin);
  const ro = readOnlyMode(page);
  // Twin first: a read-write tab offers it.
  await dragFile(page, "dropped_writable.csv", false);
  await expect(page.getByTestId("upload-drop-overlay")).toContainText("Drop the file to upload it into a table");
  await dragFile(page, "dropped_writable.csv", true);
  const dialog = page.getByTestId("upload-data-dialog");
  await expect(dialog.getByLabel("Table", { exact: true })).toHaveValue("dropped_writable");
  await dialog.getByRole("button", { name: "Close", exact: true }).last().click();
  await expect(dialog).toHaveCount(0);

  await runDone(page, "SELECT 1 AS n");
  await ro.box.click();
  await expect(ro.mode).toHaveAttribute("data-state", "on");
  await expect(activeTab(page).getByTestId("upload-button")).toBeDisabled();
  await expect(activeTab(page).getByTestId("upload-button")).toHaveAttribute(
    "title",
    "Upload is off in a read-only tab",
  );
  await expect(page.getByTestId("explorer-upload")).toBeDisabled();
  await expect(page.getByTestId("explorer-upload")).toHaveAttribute("title", "Upload is off in a read-only tab");
  await dragFile(page, "dropped_read_only.csv", false);
  await expect(page.getByTestId("upload-drop-off")).toHaveText("Upload is off in a read-only tab");
  await dragFile(page, "dropped_read_only.csv", true);
  await expect(page.getByTestId("upload-drop-overlay")).toHaveCount(0);
  await expect(dialog).toHaveCount(0);
});

test("the schema browser offers Upload only where its session can write", async ({ page, harness }) => {
  const remote = harness.remotes.plain;
  const tree = page.getByRole("tree", { name: "Servers" });
  const off = "Upload is off: this connection opens read-only sessions";
  const addServer = async (readOnly: boolean) => {
    await page.getByTestId("explorer-add-server").click();
    const dialog = page.getByTestId("add-server-dialog");
    await dialog.getByLabel("Display name", { exact: true }).fill("Uploads");
    await dialog.getByLabel("Host", { exact: true }).fill(remote.url);
    await dialog.getByLabel("Username", { exact: true }).fill(remote.admin.username);
    await dialog.getByLabel("Password", { exact: true }).fill(remote.admin.password);
    await dialog.getByRole("checkbox", { name: "Read-only sessions", exact: true }).setChecked(readOnly);
    await dialog.getByTestId("add-server-connect").click();
    await expect(dialog).toHaveCount(0);
  };
  /** The "Upload data…" item of a tree row's menu (opened with a right click). */
  const uploadItem = async (row: Locator) => {
    await row.locator(":scope > .tree-row-wrap").click({ button: "right" });
    return page.getByRole("menuitem", { name: "Upload data…" });
  };
  const server = tree.getByRole("treeitem", { name: "Uploads", exact: true });
  const catalog = server.getByRole("treeitem", { name: "e2e", exact: true });
  const schema = catalog.getByRole("treeitem", { name: "main", exact: true });
  const serverItemOf = async () => {
    const row = server.locator(":scope > .tree-row-wrap");
    await openRowMenu(row, "More for Uploads");
    return page.getByRole("menuitem", { name: "Upload data…" });
  };

  await signIn(page, harness.admin);
  // This server, read-write: offered.
  const local = tree
    .getByRole("treeitem", { name: "This server", exact: true })
    .getByRole("treeitem", { name: harness.catalog, exact: true });
  await expect(await uploadItem(local)).toBeEnabled();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("explorer-upload")).toBeEnabled();

  // A server added read-write: offered on the server, its catalogs and its schemas.
  await addServer(false);
  await expect(await serverItemOf()).toBeEnabled();
  await page.keyboard.press("Escape");
  await expect(catalog).toBeVisible();
  await expect(await uploadItem(catalog)).toBeEnabled();
  await page.keyboard.press("Escape");

  // The same server with Read-only sessions: not offered anywhere, with the reason.
  await addServer(true);
  const item = await serverItemOf();
  await expect(item).toBeDisabled();
  await expect(item).toHaveAttribute("title", off);
  await page.keyboard.press("Escape");
  await expect(catalog).toBeVisible();
  const catalogItem = await uploadItem(catalog);
  await expect(catalogItem).toBeDisabled();
  await expect(catalogItem).toHaveAttribute("title", off);
  await page.keyboard.press("Escape");
  await catalog.getByRole("button", { name: "e2e", exact: true }).click();
  await expect(schema).toBeVisible();
  const schemaItem = await uploadItem(schema);
  await expect(schemaItem).toBeDisabled();
  await expect(schemaItem).toHaveAttribute("title", off);
  await page.keyboard.press("Escape");
  // This server's entries are unaffected.
  await expect(await uploadItem(local)).toBeEnabled();
  await page.keyboard.press("Escape");

  // A tab on that connection: its Upload, the browser's Upload button and a drop are off too.
  await activeTab(page).getByLabel("Connection", { exact: true }).selectOption({ label: "Uploads" });
  await expect(activeTab(page).getByTestId("upload-button")).toBeDisabled();
  await expect(activeTab(page).getByTestId("upload-button")).toHaveAttribute("title", off);
  await expect(page.getByTestId("explorer-upload")).toBeDisabled();
  await expect(page.getByTestId("explorer-upload")).toHaveAttribute("title", off);
  await dragFile(page, "dropped_remote.csv", false);
  await expect(page.getByTestId("upload-drop-off")).toHaveText(off);
  await dragFile(page, "dropped_remote.csv", true);
  await expect(page.getByTestId("upload-data-dialog")).toHaveCount(0);
});

test("while the server is asked, the box is pending, not checked", async ({ page, harness }) => {
  await signIn(page, harness.admin);
  const ro = readOnlyMode(page);
  await runDone(page, "SELECT 1 AS n");
  // Hold the switch until the box has been looked at.
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  const sessionDoc = /\/chargewire\/v1\/sessions\/[^/?]+$/;
  await page.route(sessionDoc, async (route) => {
    if (route.request().method() !== "PATCH") return route.fallback();
    await held;
    await route.fallback();
  });
  await ro.box.click();
  await expect.poll(async () => ro.mode.getAttribute("data-state")).toBe("pending");
  expect(await ro.box.isChecked()).toBe(false);
  expect(await ro.box.evaluate((input: HTMLInputElement) => input.indeterminate)).toBe(true);
  expect(await ro.box.isEnabled()).toBe(false);
  await expect(ro.mode).toContainText("Confirming with the server…");
  release();
  await expect(ro.mode).toHaveAttribute("data-state", "on");
  await expect(ro.box).toBeChecked();
  expect(await ro.box.evaluate((input: HTMLInputElement) => input.indeterminate)).toBe(false);
  await page.unroute(sessionDoc);
});
