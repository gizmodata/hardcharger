// The UI on its own (`hardcharger ui`): no sign-in page, no local server in the tree and no
// admin pages; a server added through its relay, signed in, queried.
import { type ChildProcess, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import type { Page } from "@playwright/test";

import { cleanEnvironment, freePort } from "./harness.ts";
import { editorFocused, expect, gridRows, runDone, test } from "./fixtures.ts";

let ui: ChildProcess | undefined;
let url: string;
let log = "";

test.beforeAll(async ({}, testInfo) => {
  const state = JSON.parse(process.env["HARDCHARGER_UI_E2E_STATE"]!) as { clientBinary: string; caFile: string };
  if (!existsSync(state.clientBinary)) {
    throw new Error(
      `${state.clientBinary} is missing: HARDCHARGER_UI_TEST_CLIENT_BINARY must name the hardcharger client`,
    );
  }
  const port = await freePort();
  ui = spawn(state.clientBinary, ["ui", "--no-open", "--listen", `127.0.0.1:${port}`], {
    env: { ...cleanEnvironment(), SSL_CERT_FILE: state.caFile },
    stdio: ["ignore", "pipe", "pipe"],
  });
  ui.stdout?.on("data", (d: Buffer) => (log += d.toString()));
  ui.stderr?.on("data", (d: Buffer) => (log += d.toString()));
  url = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 30_000;
  for (;;) {
    try {
      const r = await fetch(`${url}/ui/v1/app`);
      if (r.ok) break;
    } catch {
      // not listening yet
    }
    if (ui.exitCode !== null) throw new Error(`hardcharger ui exited ${ui.exitCode}:\n${log}`);
    if (Date.now() > deadline) throw new Error(`hardcharger ui did not come up:\n${log}`);
    await new Promise((r) => setTimeout(r, 200));
  }
  testInfo.annotations.push({ type: "standalone-ui", description: url });
});

test.afterAll(async () => {
  if (ui && ui.exitCode === null) {
    ui.kill("SIGTERM");
    await new Promise((r) => ui!.once("exit", r));
  }
});

async function open(page: Page): Promise<void> {
  await page.goto(`${url}/ui/`);
  await expect(page.getByTestId("header-no-server")).toHaveText("No local server");
}

test("opens without a sign-in, hides what needs a local server, and says to add one", async ({ page }) => {
  await open(page);
  await expect(page.getByLabel("User name")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Sign out" })).toHaveCount(0);
  for (const name of ["Sessions", "SQL Monitor", "Server Configuration", "API"]) {
    await expect(page.getByRole("button", { name, exact: true })).toHaveCount(0);
  }
  await expect(page.getByTestId("explorer-no-servers")).toContainText("No servers yet");
  await expect(page.getByRole("tree", { name: "Servers" }).getByRole("treeitem", { name: "This server" })).toHaveCount(
    0,
  );
});

test("a server added through the relay is signed in through it, listed and queried", async ({ page, harness }) => {
  await open(page);
  await page.getByTestId("explorer-no-servers").getByRole("button", { name: "Add a server" }).click();
  const dialog = page.getByTestId("add-server-dialog");
  await dialog.getByLabel("Display name", { exact: true }).fill("Signed");
  // An https:// URL pasted into Host fills host, port and TLS.
  await dialog.getByLabel("Host", { exact: true }).fill(harness.remotes.signed.url);
  await expect(dialog.getByLabel("Port", { exact: true })).toHaveValue(new URL(harness.remotes.signed.url).port);
  await expect(dialog.getByLabel("Use TLS", { exact: true })).toBeChecked();
  await dialog.getByLabel("Username", { exact: true }).fill(harness.remotes.signed.admin.username);
  await dialog.getByLabel("Password", { exact: true }).fill(harness.remotes.signed.admin.password);
  await dialog.getByTestId("add-server-connect").click();
  await expect(dialog).toHaveCount(0);
  const item = page.getByRole("tree", { name: "Servers" }).getByRole("treeitem", { name: "Signed", exact: true });
  await expect(item).toHaveAttribute("data-connected", "true");
  await expect(item.locator(".tls-lock").first()).toHaveAttribute("data-tls", "verified");
  await expect(item.getByRole("treeitem", { name: "e2e", exact: true })).toBeVisible();
  // A tab opens on it (the only server), and runs there.
  await page.getByTestId("new-tab-on").click();
  await page.getByRole("menuitem", { name: "Signed", exact: true }).click();
  await editorFocused(page);
  await runDone(page, "SELECT count(*) AS n FROM remote");
  expect((await gridRows(page))[1]).toEqual(["3"]);
  // The relay session the UI holds is the one the relay issued (no sign-in of its own).
  const relaySession = await page.evaluate(() => sessionStorage.getItem("hardcharger-ui:relay-session:v1"));
  expect(relaySession).toMatch(/^p-[0-9a-f]+$/);
});
