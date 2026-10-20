#!/usr/bin/env node
// SheetJS Community Edition (the `xlsx` package, which reads Excel workbooks for Upload data)
// is published on SheetJS's own CDN, not npm: npm's `xlsx` stopped at 0.18.5, which has known
// vulnerabilities (CVE-2023-30533, CVE-2024-22363, fixed in 0.19.3 and 0.20.2). vendor/ holds
// the tarball of the pinned release, vendor/sheetjs.json its source URL and SHA-256, and
// package-lock.json pins its hash again. Nothing is fetched at build or run time.
//
//   node scripts/vendor-sheetjs.mjs --verify            the tarball matches the pin
//   node scripts/vendor-sheetjs.mjs --verify --fetch    ...and is what the CDN serves for it

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import { parseArgs } from "node:util";

const root = path.resolve(import.meta.dirname, "..");
const pin = JSON.parse(readFileSync(path.join(root, "vendor", "sheetjs.json"), "utf8"));
const { values } = parseArgs({ options: { verify: { type: "boolean" }, fetch: { type: "boolean" } } });
if (!values.verify) {
  console.error("usage: vendor-sheetjs.mjs --verify [--fetch]");
  process.exit(2);
}
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const actual = sha256(readFileSync(path.join(root, "vendor", pin.tarball)));
if (actual !== pin.sha256) {
  console.error(`vendor/${pin.tarball} has SHA-256 ${actual}, but vendor/sheetjs.json pins ${pin.sha256}`);
  process.exit(1);
}
console.log(`vendor/${pin.tarball} matches its pin (${pin.package} ${pin.version})`);
if (values.fetch) {
  const resp = await fetch(pin.source);
  if (!resp.ok) {
    console.error(`${pin.source} answered ${resp.status}`);
    process.exit(1);
  }
  const served = sha256(Buffer.from(await resp.arrayBuffer()));
  if (served !== pin.sha256) {
    console.error(`${pin.source} serves SHA-256 ${served}, not the pinned ${pin.sha256}`);
    process.exit(1);
  }
  console.log(`${pin.source} serves the same bytes`);
}
