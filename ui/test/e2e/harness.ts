// Starts what the e2e tests run against: hardcharger-server (the binary named by
// HARDCHARGER_UI_TEST_SERVER_BINARY) with access control on (PostgreSQL at
// HARDCHARGER_UI_TEST_RBAC_STORE_URI), seeded with data, a tenant and a non-admin user;
// and the built UI served at /ui/ on one origin with /chargewire passed through to the server.
// Anything missing fails the run; nothing is skipped.

import { type ChildProcess, spawn } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import https from "node:https";
import type { Server } from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";

import { connect } from "@gizmodata/chargewire";

import { createUiServer } from "../../scripts/serve.mjs";
import { makePki } from "./pki.ts";

export interface RemoteServer {
  url: string;
  admin: { username: string; password: string };
  /** The server's certificate's SHA-256 (aa:bb:…), for the ones with TLS. */
  fingerprint?: string;
}

export interface HarnessState {
  url: string;
  serverUrl: string;
  admin: { username: string; password: string };
  /** A tenant user holding two roles: readers (orders) and billing (invoices). */
  user: { username: string; password: string; catalog: string; roles: string[] };
  hiddenCatalog: string;
  catalog: string;
  /**
   * Other servers the UI can add through the relay, each with a `remote` table of 3 rows:
   * plain HTTP; TLS with a certificate the UI's server trusts (its CA is in SSL_CERT_FILE);
   * and TLS with a self-signed certificate, to be trusted by its fingerprint.
   */
  remotes: { plain: RemoteServer; signed: RemoteServer; selfSigned: RemoteServer };
  /** The CA file the UI's server trusts (for a standalone UI started with the same trust). */
  caFile: string;
  /** The `hardcharger` client binary beside the server's (for `hardcharger ui`). */
  clientBinary: string;
}

export const stateVariable = "HARDCHARGER_UI_E2E_STATE";

export function harnessState(): HarnessState {
  const raw = process.env[stateVariable];
  if (!raw) throw new Error(`${stateVariable} is not set: the tests run through playwright.config.ts's global setup`);
  return JSON.parse(raw) as HarnessState;
}

export async function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.once("error", reject);
    s.listen(0, "127.0.0.1", () => {
      const { port } = s.address() as net.AddressInfo;
      s.close(() => resolve(port));
    });
  });
}

// The server reads HARDCHARGER_* (and GizmoSQL 1.x's GIZMOSQL_*) settings from its
// environment; the developer's shell must not leak into the test server.
export function cleanEnvironment(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (!k.startsWith("HARDCHARGER_") && !k.startsWith("GIZMOSQL_")) env[k] = v;
  }
  return env;
}

export interface Harness {
  state: HarnessState;
  stop: () => Promise<void>;
}

// HARDCHARGER_UI_TEST_EMBEDDED=1: the server serves the UI itself (its web_ui option, in a
// build that embeds a hardcharger-ui tarball) instead of scripts/serve.mjs serving dist/.
const embedded = process.env["HARDCHARGER_UI_TEST_EMBEDDED"] === "1";

export async function startHarness(): Promise<Harness> {
  const binary = process.env["HARDCHARGER_UI_TEST_SERVER_BINARY"];
  if (!binary) {
    throw new Error(
      "HARDCHARGER_UI_TEST_SERVER_BINARY must name a hardcharger-server binary (e.g. /opt/hardcharger/bin/hardcharger-server " +
        "from ghcr.io/gizmodata/hardcharger:main); the e2e tests do not run without one",
    );
  }
  if (!existsSync(binary)) throw new Error(`HARDCHARGER_UI_TEST_SERVER_BINARY=${binary} does not exist`);
  const dist = path.resolve(import.meta.dirname, "../../dist");
  if (!embedded && !existsSync(path.join(dist, "index.html"))) {
    throw new Error("dist/index.html is missing: run npm run build first");
  }
  const storeUri =
    process.env["HARDCHARGER_UI_TEST_RBAC_STORE_URI"] ??
    "postgresql://hardcharger:hardcharger@127.0.0.1:55460/hardcharger";

  // The other servers (plain, TLS signed by a CA the UI's server trusts, TLS self-signed),
  // started first so the UI's server can be given the CA as a trusted root.
  const pkiDir = mkdtempSync(path.join(os.tmpdir(), "hardcharger-ui-e2e-pki-"));
  const pki = await makePki(pkiDir);
  const selfSigned = await makeSelfSigned(pkiDir);
  const remoteSeed = ["CREATE TABLE remote AS SELECT range AS id, 'remote row ' || range AS note FROM range(3)"];
  const remotePassword = () => `rm:${randomBytes(6).toString("hex")}@e2e`;
  const plainPassword = remotePassword();
  const plain = await startServer({
    binary,
    adminPassword: plainPassword,
    extraArgs: ["--init-sql-commands", remoteSeed.join(";")],
  });
  const signedPassword = remotePassword();
  const signed = await startServer({
    binary,
    adminPassword: signedPassword,
    extraArgs: [
      "--init-sql-commands",
      remoteSeed.join(";"),
      "--tls-cert",
      pki.server.cert,
      "--tls-key",
      pki.server.key,
    ],
    tls: pki.ca,
  });
  const selfPassword = remotePassword();
  const self = await startServer({
    binary,
    adminPassword: selfPassword,
    extraArgs: [
      "--init-sql-commands",
      remoteSeed.join(";"),
      "--tls-cert",
      selfSigned.cert,
      "--tls-key",
      selfSigned.key,
    ],
    tls: selfSigned.ca,
  });

  // A password with ':' and '@' in it: it must survive the sign-in form and the JSON body.
  const adminPassword = `ui:${randomBytes(6).toString("hex")}@e2e`;
  const server = await startServer({
    binary,
    adminPassword,
    extraArgs: ["--rbac-store-uri", storeUri],
    env: { SSL_CERT_FILE: pki.ca },
  });
  const serverUrl = server.url;

  const suffix = randomBytes(3).toString("hex");
  const acme = `acme_${suffix}`;
  const beta = `beta_${suffix}`;
  const user = {
    username: `${acme}/joe`,
    password: `joe:${randomBytes(4).toString("hex")}@x`,
    catalog: acme,
    roles: [`${acme}/readers`, `${acme}/billing`],
  };
  const admin = await connect({ url: serverUrl, username: "admin", password: adminPassword });
  const seed = [
    "CREATE SCHEMA sales",
    "CREATE TABLE sales.orders AS SELECT range AS id, 'customer ' || (range % 7) AS customer, " +
      "(range * 1.25)::DECIMAL(12,2) AS amount, DATE '2026-01-01' + range::INTEGER AS ordered_on FROM range(2500)",
    "CREATE TABLE sales.tricky (id INTEGER, label VARCHAR, note VARCHAR)",
    "INSERT INTO sales.tricky VALUES (1, 'plain', 'nothing odd'), (2, 'tab' || chr(9) || 'here', 'say \"hi\"'), " +
      "(3, NULL, 'line' || chr(10) || 'break')",
    "CREATE VIEW sales.big_orders AS SELECT * FROM sales.orders WHERE amount > 1000",
    `ATTACH ':memory:' AS ${acme}`,
    `ATTACH ':memory:' AS ${beta}`,
    `CREATE TABLE ${acme}.main.orders AS SELECT range AS id, 'acme order ' || range AS note FROM range(5)`,
    `CREATE TABLE ${acme}.main.payroll AS SELECT 1 AS id, 'not granted' AS note`,
    `CREATE TABLE ${beta}.main.secrets AS SELECT 1 AS id, 'beta-secret' AS hush`,
    `CREATE TENANT ${acme}`,
    `CREATE TENANT ${beta}`,
    `ALTER CATALOG ${acme} SET TENANT ${acme}`,
    `ALTER CATALOG ${beta} SET TENANT ${beta}`,
    `ALTER TENANT ${acme} SET DEFAULT CATALOG ${acme}`,
    `CREATE USER ${user.username} WITH PASSWORD '${user.password}'`,
    `CREATE ROLE ${acme}/readers`,
    `GRANT SELECT ON ${acme}.main.orders TO ${acme}/readers`,
    `GRANT ${acme}/readers TO ${user.username}`,
    `CREATE TABLE ${acme}.main.invoices AS SELECT range AS id, range * 10 AS total FROM range(2)`,
    `CREATE ROLE ${acme}/billing`,
    `GRANT SELECT ON ${acme}.main.invoices TO ${acme}/billing`,
    `GRANT ${acme}/billing TO ${user.username}`,
  ];
  for (const sql of seed) await admin.execute(sql);
  const catalog = String((await admin.query("SELECT current_database() AS c")).get(0)!.toJSON()["c"]);
  await admin.close();

  const ui: Server | undefined = embedded ? undefined : createUiServer({ dist, upstream: serverUrl });
  const uiUrl = ui
    ? `http://127.0.0.1:${await new Promise<number>((resolve) =>
        ui.listen(0, "127.0.0.1", () => resolve((ui.address() as net.AddressInfo).port)),
      )}`
    : serverUrl;

  // Access-control changes reach the server's sessions within about a second (it polls).
  await new Promise((r) => setTimeout(r, 1500));

  const clientBinary =
    process.env["HARDCHARGER_UI_TEST_CLIENT_BINARY"] ??
    path.join(path.dirname(binary), process.platform === "win32" ? "hardcharger.exe" : "hardcharger");
  const state: HarnessState = {
    url: uiUrl,
    serverUrl,
    admin: { username: "admin", password: adminPassword },
    user,
    hiddenCatalog: beta,
    catalog,
    remotes: {
      plain: { url: plain.url, admin: { username: "admin", password: plainPassword } },
      signed: {
        url: signed.url,
        admin: { username: "admin", password: signedPassword },
        fingerprint: fingerprintOf(pki.server.cert),
      },
      selfSigned: {
        url: self.url,
        admin: { username: "admin", password: selfPassword },
        fingerprint: fingerprintOf(selfSigned.cert),
      },
    },
    caFile: pki.ca,
    clientBinary,
  };

  return {
    state,
    stop: async () => {
      try {
        const a = await connect({ url: serverUrl, username: "admin", password: adminPassword });
        for (const sql of [
          `DROP USER ${user.username}`,
          `DROP ROLE ${acme}/readers`,
          `DROP ROLE ${acme}/billing`,
          `ALTER CATALOG ${acme} UNSET TENANT`,
          `ALTER CATALOG ${beta} UNSET TENANT`,
          `DROP TENANT ${acme}`,
          `DROP TENANT ${beta}`,
        ]) {
          await a.execute(sql).catch(() => {});
        }
        await a.close();
      } catch {
        // the store keeps a few names; they don't collide with the next run's
      }
      if (ui) await new Promise<void>((resolve) => ui.close(() => resolve()));
      await server.stop();
      await Promise.all([plain.stop(), signed.stop(), self.stop()]);
      rmSync(pkiDir, { recursive: true, force: true });
    },
  };
}

/** A certificate's SHA-256 fingerprint as the relay shows it (aa:bb:…). */
export function fingerprintOf(pemFile: string): string {
  const pem = readFileSync(pemFile, "utf8");
  const der = Buffer.from(pem.replace(/-----[A-Z ]+-----/g, "").replace(/\s+/g, ""), "base64");
  return createHash("sha256").update(der).digest("hex").match(/.{2}/g)!.join(":");
}

/** A self-signed server certificate for 127.0.0.1 (nothing trusts it but its fingerprint). */
async function makeSelfSigned(dir: string): Promise<{ cert: string; key: string; ca: string }> {
  // A server certificate from a throwaway CA nothing trusts (the UI's server gets the other
  // CA as its root): as good as self-signed to the relay, which can only trust it by its
  // fingerprint. `ca` is for the harness's own health check of that server.
  mkdirSync(path.join(dir, "self"), { recursive: true });
  const own = await makePki(path.join(dir, "self"));
  return { ...own.server, ca: own.ca };
}

interface RunningServer {
  url: string;
  stop: () => Promise<void>;
}

/**
 * Starts hardcharger-server on a free port with an empty database of its own, and waits
 * until it is healthy.
 */
async function startServer(o: {
  binary: string;
  adminPassword: string;
  extraArgs: string[];
  port?: number;
  env?: NodeJS.ProcessEnv;
  /** A server with TLS: the PEM to trust when waiting for it (its URL is then https://). */
  tls?: string;
}): Promise<RunningServer> {
  const work = mkdtempSync(path.join(os.tmpdir(), "hardcharger-ui-e2e-"));
  const port = o.port ?? (await freePort());
  const args = [
    "--host",
    "127.0.0.1",
    "--port",
    String(port),
    "--health-port",
    "0",
    "--admin-username",
    "admin",
    "--database",
    path.join(work, "e2e.duckdb"),
    "--graceful-shutdown",
    "false",
    "--print-queries",
    "false",
    // The UI's own endpoints (its contract, the relay to other servers) live on the server
    // with the web UI on, whether the page comes from the server or from scripts/serve.mjs.
    "--web-ui",
    ...o.extraArgs,
  ];
  const child: ChildProcess = spawn(o.binary, args, {
    env: { ...cleanEnvironment(), ...o.env, HARDCHARGER_ADMIN_PASSWORD: o.adminPassword },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let log = "";
  child.stdout?.on("data", (d: Buffer) => (log += d.toString()));
  child.stderr?.on("data", (d: Buffer) => (log += d.toString()));
  let exited = false;
  child.on("exit", () => (exited = true));

  const url = `${o.tls ? "https" : "http"}://127.0.0.1:${port}`;
  const healthy = (): Promise<boolean> =>
    o.tls
      ? new Promise((resolve) => {
          const req = https.get(`${url}/chargewire/v1/health`, { ca: readFileSync(o.tls!) }, (res) => {
            res.resume();
            resolve(res.statusCode === 200);
          });
          req.on("error", () => resolve(false));
        })
      : fetch(`${url}/chargewire/v1/health`).then((r) => r.ok);
  const deadline = Date.now() + 30_000;
  for (;;) {
    if (exited) throw new Error(`hardcharger-server exited at startup:\n${log.slice(-3000)}`);
    try {
      if (await healthy()) break;
    } catch {
      // not listening yet
    }
    if (Date.now() > deadline) {
      child.kill("SIGKILL");
      throw new Error(`hardcharger-server did not become healthy in 30 s:\n${log.slice(-3000)}`);
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  return {
    url,
    stop: async () => {
      if (!exited) {
        child.kill("SIGTERM");
        const t = setTimeout(() => child.kill("SIGKILL"), 5000);
        await new Promise((r) => child.once("exit", r));
        clearTimeout(t);
      }
      rmSync(work, { recursive: true, force: true });
    },
  };
}

export interface IdleServer {
  /** Where the UI is (the server itself when it embeds the UI). */
  url: string;
  admin: { username: string; password: string };
  stop: () => Promise<void>;
}

/**
 * A second server, without access control, whose sessions end after `idleSeconds` idle
 * (its sweep runs every 5 s): for the tests of a session the server evicted, which the
 * shared server's default (15 minutes) would make wait too long.
 */
export async function startIdleServer(idleSeconds: number): Promise<IdleServer> {
  return startOwnServer(["--session-idle-timeout", String(idleSeconds)]);
}

/**
 * A second server, without access control, started with `extraArgs` and `env` (HARDCHARGER_*
 * options): for the tests of a server option the shared server doesn't set.
 */
export async function startOwnServer(extraArgs: string[], env: NodeJS.ProcessEnv = {}): Promise<IdleServer> {
  const binary = process.env["HARDCHARGER_UI_TEST_SERVER_BINARY"]!;
  const adminPassword = `ui:${randomBytes(6).toString("hex")}@own`;
  const server = await startServer({ binary, adminPassword, extraArgs, env });
  const dist = path.resolve(import.meta.dirname, "../../dist");
  const ui: Server | undefined = embedded ? undefined : createUiServer({ dist, upstream: server.url });
  const url = ui
    ? `http://127.0.0.1:${await new Promise<number>((resolve) =>
        ui.listen(0, "127.0.0.1", () => resolve((ui.address() as net.AddressInfo).port)),
      )}`
    : server.url;
  return {
    url,
    admin: { username: "admin", password: adminPassword },
    stop: async () => {
      if (ui) await new Promise<void>((resolve) => ui.close(() => resolve()));
      await server.stop();
    },
  };
}

export interface OAuthServer {
  /** Where the UI is (the server itself when it embeds the UI). */
  url: string;
  stop: () => Promise<void>;
}

/**
 * A server with browser sign-in (Chargewire §4.3) through the mock identity provider `idp`,
 * without access control: its users sign in as administrators. The server's public URL
 * (oauth_base_url) is the UI's origin, which passes /chargewire through, so the provider's
 * redirect reaches the server's callback there and the page comes back to the UI.
 */
export async function startOAuthServer(idp: {
  issuer: string;
  clientId: string;
  clientSecret: string;
}): Promise<OAuthServer> {
  const binary = process.env["HARDCHARGER_UI_TEST_SERVER_BINARY"]!;
  const port = await freePort();
  const serverUrl = `http://127.0.0.1:${port}`;
  const dist = path.resolve(import.meta.dirname, "../../dist");
  const ui: Server | undefined = embedded ? undefined : createUiServer({ dist, upstream: serverUrl });
  const url = ui
    ? `http://127.0.0.1:${await new Promise<number>((resolve) =>
        ui.listen(0, "127.0.0.1", () => resolve((ui.address() as net.AddressInfo).port)),
      )}`
    : serverUrl;
  const server = await startServer({
    binary,
    adminPassword: `ui:${randomBytes(6).toString("hex")}@oauth`,
    port,
    env: { HARDCHARGER_OAUTH_CLIENT_SECRET: idp.clientSecret },
    extraArgs: [
      "--token-allowed-issuer",
      idp.issuer,
      "--token-allowed-audience",
      idp.clientId,
      "--token-default-role",
      "admin",
      "--token-authorized-emails",
      "*@example.com",
      "--oauth-client-id",
      idp.clientId,
      "--oauth-base-url",
      url,
      "--oauth-provider-name",
      "Mock IdP",
    ],
  });
  return {
    url,
    stop: async () => {
      if (ui) await new Promise<void>((resolve) => ui.close(() => resolve()));
      await server.stop();
    },
  };
}
