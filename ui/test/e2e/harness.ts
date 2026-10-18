// Starts what the e2e tests run against: hardcharger-server (the binary named by
// HARDCHARGER_UI_TEST_SERVER_BINARY) with access control on (PostgreSQL at
// HARDCHARGER_UI_TEST_RBAC_STORE_URI), seeded with data, a tenant and a non-admin user;
// and the built UI served at /ui/ on one origin with /warp passed through to the server.
// Anything missing fails the run; nothing is skipped.

import { type ChildProcess, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import type { Server } from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";

import { connect } from "@gizmodata/warp-sql";

import { createUiServer } from "../../scripts/serve.mjs";

export interface HarnessState {
  url: string;
  serverUrl: string;
  admin: { username: string; password: string };
  user: { username: string; password: string; catalog: string };
  hiddenCatalog: string;
  catalog: string;
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

  const work = mkdtempSync(path.join(os.tmpdir(), "hardcharger-ui-e2e-"));
  const port = await freePort();
  // A password with ':' and '@' in it: it must survive the sign-in form and the JSON body.
  const adminPassword = `ui:${randomBytes(6).toString("hex")}@e2e`;
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
    "--rbac-store-uri",
    storeUri,
    "--graceful-shutdown",
    "false",
    "--print-queries",
    "false",
    ...(embedded ? ["--web-ui"] : []),
  ];
  const child: ChildProcess = spawn(binary, args, {
    env: { ...cleanEnvironment(), HARDCHARGER_ADMIN_PASSWORD: adminPassword },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let log = "";
  child.stdout?.on("data", (d: Buffer) => (log += d.toString()));
  child.stderr?.on("data", (d: Buffer) => (log += d.toString()));
  let exited = false;
  child.on("exit", () => (exited = true));

  const serverUrl = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 30_000;
  for (;;) {
    if (exited) throw new Error(`hardcharger-server exited at startup:\n${log.slice(-3000)}`);
    try {
      const r = await fetch(`${serverUrl}/warp/v1/health`);
      if (r.ok) break;
    } catch {
      // not listening yet
    }
    if (Date.now() > deadline) {
      child.kill("SIGKILL");
      throw new Error(`hardcharger-server did not become healthy in 30 s:\n${log.slice(-3000)}`);
    }
    await new Promise((r) => setTimeout(r, 200));
  }

  const suffix = randomBytes(3).toString("hex");
  const acme = `acme_${suffix}`;
  const beta = `beta_${suffix}`;
  const user = { username: `${acme}/joe`, password: `joe:${randomBytes(4).toString("hex")}@x`, catalog: acme };
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

  const state: HarnessState = {
    url: uiUrl,
    serverUrl,
    admin: { username: "admin", password: adminPassword },
    user,
    hiddenCatalog: beta,
    catalog,
  };

  return {
    state,
    stop: async () => {
      try {
        const a = await connect({ url: serverUrl, username: "admin", password: adminPassword });
        for (const sql of [
          `DROP USER ${user.username}`,
          `DROP ROLE ${acme}/readers`,
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
