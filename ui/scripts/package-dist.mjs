#!/usr/bin/env node
// Packs dist/ as release/hardcharger-ui-<version>.tar.gz, with release/SHA256SUMS.
//
// The tarball holds one directory, hardcharger-ui/, with the built files and MANIFEST: a
// line "<sha256>  <path>" per file, sorted by path. The SHA-256 of MANIFEST (printed, and in
// release/hardcharger-ui-<version>.CONTENT_SHA256) names the build's content whatever the
// compression; hardcharger-server's build logs it when it embeds the UI. The tar itself is
// deterministic too (sorted entries, fixed times and owners).

import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { gzipSync } from "node:zlib";

import { uiVersion } from "./version.mjs";

const root = path.resolve(import.meta.dirname, "..");
const dist = path.join(root, "dist");
const out = path.join(root, "release");
const version = uiVersion();

function walk(dir, prefix = "") {
  const files = [];
  for (const name of readdirSync(dir).sort()) {
    const rel = prefix ? `${prefix}/${name}` : name;
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) files.push(...walk(full, rel));
    else files.push(rel);
  }
  return files;
}

const sha256 = (data) => createHash("sha256").update(data).digest("hex");
const files = walk(dist).sort();
if (!files.includes("index.html")) throw new Error("dist/index.html is missing: run npm run build");
const manifest = files.map((f) => `${sha256(readFileSync(path.join(dist, f)))}  ${f}\n`).join("");

function header(name, size, type) {
  const h = Buffer.alloc(512);
  let prefix = "";
  let base = name;
  if (Buffer.byteLength(name) > 100) {
    const cut = name.lastIndexOf("/", 155);
    prefix = name.slice(0, cut);
    base = name.slice(cut + 1);
    if (Buffer.byteLength(base) > 100 || Buffer.byteLength(prefix) > 155) throw new Error(`path too long: ${name}`);
  }
  h.write(base, 0, 100, "utf8");
  h.write(type === "5" ? "0000755\0" : "0000644\0", 100, 8, "ascii");
  h.write("0000000\0", 108, 8, "ascii"); // uid
  h.write("0000000\0", 116, 8, "ascii"); // gid
  h.write(`${size.toString(8).padStart(11, "0")}\0`, 124, 12, "ascii");
  h.write("00000000000\0", 136, 12, "ascii"); // mtime: 0
  h.write("        ", 148, 8, "ascii"); // checksum placeholder
  h.write(type, 156, 1, "ascii");
  h.write("ustar\0", 257, 6, "ascii");
  h.write("00", 263, 2, "ascii");
  h.write(prefix, 345, 155, "utf8");
  let sum = 0;
  for (const b of h) sum += b;
  h.write(`${sum.toString(8).padStart(6, "0")}\0 `, 148, 8, "ascii");
  return h;
}

const entries = [];
const dirs = new Set(["hardcharger-ui"]);
for (const f of files) {
  const parts = f.split("/");
  for (let i = 1; i < parts.length; i++) dirs.add(`hardcharger-ui/${parts.slice(0, i).join("/")}`);
}
const items = [
  ...[...dirs].map((d) => ({ name: `${d}/`, data: null })),
  ...files.map((f) => ({ name: `hardcharger-ui/${f}`, data: readFileSync(path.join(dist, f)) })),
  { name: "hardcharger-ui/MANIFEST", data: Buffer.from(manifest) },
].sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
for (const item of items) {
  if (item.data === null) {
    entries.push(header(item.name, 0, "5"));
  } else {
    entries.push(header(item.name, item.data.length, "0"), item.data);
    const padding = (512 - (item.data.length % 512)) % 512;
    if (padding) entries.push(Buffer.alloc(padding));
  }
}
entries.push(Buffer.alloc(1024));
const tarball = gzipSync(Buffer.concat(entries), { level: 9 });

mkdirSync(out, { recursive: true });
const name = `hardcharger-ui-${version}.tar.gz`;
writeFileSync(path.join(out, name), tarball);
writeFileSync(path.join(out, "SHA256SUMS"), `${sha256(tarball)}  ${name}\n`);
const content = sha256(manifest);
writeFileSync(path.join(out, `hardcharger-ui-${version}.CONTENT_SHA256`), `${content}\n`);
console.log(`release/${name}: ${files.length} files, ${(tarball.length / 1048576).toFixed(1)} MiB`);
console.log(`tarball sha256 ${sha256(tarball)}`);
console.log(`content sha256 ${content} (the SHA-256 of hardcharger-ui/MANIFEST)`);
