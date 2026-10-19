#!/usr/bin/env node
// Checks the build for what the server's Content-Security-Policy and caching rely on:
// index.html has no inline script or style and refers only to files that exist under /ui/,
// every file in assets/ has a content hash in its name (they are served as immutable), and
// no source maps or data: scripts slipped in.

import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";

const dist = path.resolve(process.argv[2] ?? "dist");
const problems = [];
const html = readFileSync(path.join(dist, "index.html"), "utf8");

for (const m of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)) {
  if (!/\bsrc=/.test(m[1])) problems.push("index.html has an inline <script>");
  if (m[2].trim() !== "") problems.push("index.html has a <script> with a body");
}
if (/<style\b/.test(html)) problems.push("index.html has an inline <style>");
if (/\son[a-z]+=/i.test(html)) problems.push("index.html has an inline event handler");
for (const m of html.matchAll(/\b(?:src|href)="([^"]+)"/g)) {
  const ref = m[1];
  if (/^https?:/.test(ref)) {
    problems.push(`index.html loads ${ref} from another origin`);
    continue;
  }
  if (!ref.startsWith("/ui/")) {
    problems.push(`index.html refers to ${ref}, outside /ui/`);
    continue;
  }
  try {
    readFileSync(path.join(dist, ref.slice("/ui/".length)));
  } catch {
    problems.push(`index.html refers to ${ref}, which is not in the build`);
  }
}
const files = readdirSync(path.join(dist, "assets"));
for (const f of files) {
  if (f.endsWith(".map")) problems.push(`source map in the build: assets/${f}`);
  if (!/-[A-Za-z0-9_-]{8,}\.[a-z0-9]+$/.test(f)) problems.push(`assets/${f} has no content hash in its name`);
  if (f.endsWith(".js") && /\bsrc=["']data:/.test(readFileSync(path.join(dist, "assets", f), "utf8"))) {
    problems.push(`assets/${f} loads a data: script`);
  }
}
// Inter is embedded under the SIL Open Font License, which travels with it.
if (files.some((f) => f.endsWith(".woff2"))) {
  try {
    if (!/SIL Open Font License/.test(readFileSync(path.join(dist, "fonts", "Inter-LICENSE.txt"), "utf8"))) {
      problems.push("fonts/Inter-LICENSE.txt is not the font's license");
    }
  } catch {
    problems.push("a font is embedded without fonts/Inter-LICENSE.txt");
  }
}
// The third-party notices (scripts/third-party-notices.mjs) list every package the app
// depends on at run time, each bundled, each with its licence text.
try {
  const notices = readFileSync(path.join(dist, "third-party-notices.txt"), "utf8");
  const pkg = JSON.parse(readFileSync(path.join(dist, "..", "package.json"), "utf8"));
  for (const name of Object.keys(pkg.dependencies ?? {})) {
    const section = new RegExp(`^${name.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&")} \\S+\nLicense: `, "m");
    if (!section.test(notices)) problems.push(`third-party-notices.txt has no section for ${name}`);
  }
} catch (error) {
  problems.push(`third-party-notices.txt: ${error.message}`);
}
if (problems.length) {
  for (const p of problems) console.error(`check-dist: ${p}`);
  process.exit(1);
}
console.log(`check-dist: ${files.length} assets, index.html clean`);
