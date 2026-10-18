// The UI a server build embeds, under mutual TLS (HARDCHARGER_UI_TEST_EMBEDDED=1 runs this
// file; playwright.config.ts selects it only then, since only a server can present the page
// over its own TLS listener). The browser must present a client certificate to load /ui at
// all; with certificate sign-in on, "Sign in with my certificate" signs in with it, and every
// request the page makes carries it, so the certificate-bound token works throughout.
import { type ChildProcess, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import https from "node:https";
import os from "node:os";
import path from "node:path";

import { cleanEnvironment, freePort } from "./harness.ts";
import { expect, gridRows, runSql, test, watchPage } from "./fixtures.ts";
import { type Pki, makePki } from "./pki.ts";

let dir: string;
let pki: Pki;
let ops: { cert: string; key: string };
let server: ChildProcess | undefined;
let origin: string;
const adminPassword = `mtls:${randomBytes(6).toString("hex")}@e2e`;

function getWithCertificate(url: string): Promise<number> {
  return new Promise((resolve, reject) => {
    const req = https.get(
      url,
      { ca: readFileSync(pki.ca), cert: readFileSync(ops.cert), key: readFileSync(ops.key) },
      (res) => {
        res.resume();
        resolve(res.statusCode ?? 0);
      },
    );
    req.on("error", reject);
  });
}

test.beforeAll(async () => {
  const binary = process.env["HARDCHARGER_UI_TEST_SERVER_BINARY"];
  if (!binary) throw new Error("HARDCHARGER_UI_TEST_SERVER_BINARY must name a hardcharger-server binary");
  dir = mkdtempSync(path.join(os.tmpdir(), "hardcharger-ui-mtls-"));
  pki = await makePki(dir);
  ops = await pki.client("ops@example.com");
  const port = await freePort();
  origin = `https://127.0.0.1:${port}`;
  let log = "";
  server = spawn(
    binary,
    [
      "--host",
      "127.0.0.1",
      "--port",
      String(port),
      "--health-port",
      "0",
      "--admin-username",
      "admin",
      "--database",
      ":memory:",
      "--graceful-shutdown",
      "false",
      "--print-queries",
      "false",
      "--web-ui",
      "--tls-cert",
      pki.server.cert,
      "--tls-key",
      pki.server.key,
      "--mtls-ca-cert",
      pki.ca,
      "--mtls-certificate-sign-in",
      "--mtls-admin-identities",
      "ops@example.com",
    ],
    { env: { ...cleanEnvironment(), HARDCHARGER_ADMIN_PASSWORD: adminPassword }, stdio: ["ignore", "pipe", "pipe"] },
  );
  server.stdout?.on("data", (d: Buffer) => (log += d.toString()));
  server.stderr?.on("data", (d: Buffer) => (log += d.toString()));
  const deadline = Date.now() + 30_000;
  for (;;) {
    if (server.exitCode !== null) throw new Error(`hardcharger-server exited at startup:\n${log.slice(-3000)}`);
    try {
      if ((await getWithCertificate(`${origin}/warp/v1/health`)) === 200) break;
    } catch {
      // not up yet
    }
    if (Date.now() > deadline) throw new Error(`hardcharger-server did not come up:\n${log.slice(-3000)}`);
    await new Promise((r) => setTimeout(r, 200));
  }
});

test.afterAll(async () => {
  if (server && server.exitCode === null) {
    server.kill("SIGTERM");
    await new Promise((r) => server!.once("exit", r));
  }
  rmSync(dir, { recursive: true, force: true });
});

test("with a client certificate: the page loads, certificate sign-in works, and so does a reload", async ({
  browser,
  browserName,
}) => {
  // Playwright presents client certificates through a proxy of its own, which WebKit on macOS
  // bypasses for loopback addresses; its documented way around that is the name
  // local.playwright, which Playwright resolves to this machine.
  const host =
    browserName === "webkit" && process.platform === "darwin"
      ? origin.replace("127.0.0.1", "local.playwright")
      : origin;
  const context = await browser.newContext({
    ignoreHTTPSErrors: true, // the test CA's server certificate
    clientCertificates: [{ origin: host, certPath: ops.cert, keyPath: ops.key }],
  });
  const page = await context.newPage();
  const check = await watchPage(page);
  await page.goto(`${host}/ui/`);
  await expect(page.getByTestId("server-version")).toContainText("Hardcharger");
  await page.getByRole("button", { name: "Sign in with my certificate" }).click();
  await expect(page.getByTestId("header-user")).toContainText("ops@example.com");
  await expect(page.getByTestId("header-user")).toContainText("admin");
  await runSql(page, "SELECT HARDCHARGER_USER() AS me, 42 AS answer");
  expect((await gridRows(page))[1]).toEqual(["ops@example.com", "42"]);
  // The schema browser's metadata calls carry the certificate too.
  await expect(page.getByRole("treeitem", { name: "memory", exact: true })).toBeVisible();
  // A returning visitor: the bound token from sessionStorage, over new connections.
  await page.reload();
  await expect(page.getByTestId("header-user")).toContainText("ops@example.com");
  // The password sign-in still works beside it (mtls_require_identity_match is off).
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.getByLabel("User name").fill("admin");
  await page.getByLabel("Password").fill(adminPassword);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByTestId("header-user")).toContainText("admin");
  check();
  await context.close();
});

test("without a client certificate the page never loads", async ({ browser }) => {
  const context = await browser.newContext({ ignoreHTTPSErrors: true });
  const page = await context.newPage();
  await expect(page.goto(`${origin}/ui/`)).rejects.toThrow();
  await context.close();
});
