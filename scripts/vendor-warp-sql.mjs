#!/usr/bin/env node
// The JavaScript client, @gizmodata/warp-sql, is not on npm: the UI depends on the package
// that `npm pack` makes from gizmodata/warp-sql-js at a pinned commit, kept in vendor/ (and
// pinned again by package-lock.json's integrity hash). vendor/warp-sql-js.json names the
// commit and the tarball's SHA-256.
//
//   node scripts/vendor-warp-sql.mjs --verify             the tarball matches the pin
//   node scripts/vendor-warp-sql.mjs --verify --rebuild   ...and is what the commit builds to
//   node scripts/vendor-warp-sql.mjs --update <commit>    build <commit> and vendor it
//
// --rebuild and --update clone HARDCHARGER_UI_WARP_SQL_JS_GIT (default
// git@github.com:gizmodata/warp-sql-js.git; a local checkout's path works too).

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync, copyFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { parseArgs } from "node:util";

const root = path.resolve(import.meta.dirname, "..");
const pinFile = path.join(root, "vendor", "warp-sql-js.json");
const pin = JSON.parse(readFileSync(pinFile, "utf8"));
const { values } = parseArgs({
  options: { verify: { type: "boolean" }, rebuild: { type: "boolean" }, update: { type: "string" } },
});
const source = process.env.HARDCHARGER_UI_WARP_SQL_JS_GIT ?? `git@github.com:${pin.repository}.git`;
const sha256 = (file) => createHash("sha256").update(readFileSync(file)).digest("hex");
const run = (cmd, args, cwd) => execFileSync(cmd, args, { cwd, stdio: ["ignore", "pipe", "inherit"] }).toString();

function build(commit) {
  const dir = mkdtempSync(path.join(os.tmpdir(), "warp-sql-js-"));
  run("git", ["init", "-q", dir]);
  run("git", ["fetch", "-q", "--depth", "1", source, commit], dir);
  run("git", ["checkout", "-q", "FETCH_HEAD"], dir);
  const head = run("git", ["rev-parse", "HEAD"], dir).trim();
  if (head !== commit) throw new Error(`fetched ${head}, not ${commit}`);
  run("npm", ["ci", "--no-audit", "--no-fund", "--silent"], dir);
  run("npm", ["run", "build", "--silent"], dir);
  const name = run("npm", ["pack", "--silent"], dir).trim().split("\n").pop();
  return { dir, tarball: path.join(dir, name), name };
}

function files(dir, prefix = "") {
  const out = new Map();
  for (const n of readdirSync(dir).sort()) {
    const full = path.join(dir, n);
    const rel = prefix ? `${prefix}/${n}` : n;
    if (statSync(full).isDirectory()) for (const [k, v] of files(full, rel)) out.set(k, v);
    else out.set(rel, createHash("sha256").update(readFileSync(full)).digest("hex"));
  }
  return out;
}

function unpack(tarball) {
  const dir = mkdtempSync(path.join(os.tmpdir(), "warp-sql-pack-"));
  run("tar", ["-xzf", tarball, "-C", dir]);
  return files(dir);
}

const vendored = path.join(root, "vendor", pin.tarball);

if (values.update) {
  const commit = values.update;
  const built = build(commit);
  const target = path.join(root, "vendor", built.name);
  copyFileSync(built.tarball, target);
  if (built.name !== pin.tarball) rmSync(vendored, { force: true });
  writeFileSync(
    pinFile,
    `${JSON.stringify({ ...pin, commit, tarball: built.name, sha256: sha256(target) }, null, 2)}\n`,
  );
  rmSync(built.dir, { recursive: true, force: true });
  console.log(`vendored ${built.name} from ${pin.repository}@${commit}; now run: npm install`);
  process.exit(0);
}

if (!values.verify) {
  console.error("usage: vendor-warp-sql.mjs --verify [--rebuild] | --update <commit>");
  process.exit(2);
}
const actual = sha256(vendored);
if (actual !== pin.sha256) {
  console.error(`vendor/${pin.tarball} has SHA-256 ${actual}, but vendor/warp-sql-js.json pins ${pin.sha256}`);
  process.exit(1);
}
console.log(`vendor/${pin.tarball} matches its pin (${pin.repository}@${pin.commit.slice(0, 12)})`);
if (values.rebuild) {
  const built = build(pin.commit);
  const a = unpack(vendored);
  const b = unpack(built.tarball);
  const differ = [...new Set([...a.keys(), ...b.keys()])].filter((k) => a.get(k) !== b.get(k));
  rmSync(built.dir, { recursive: true, force: true });
  if (differ.length) {
    console.error(`the vendored package is not what ${pin.commit} builds to; these files differ:`);
    for (const k of differ) console.error(`  ${k}`);
    process.exit(1);
  }
  console.log(`rebuilt ${pin.repository}@${pin.commit.slice(0, 12)}: every file matches the vendored package`);
}
