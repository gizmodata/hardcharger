// The web UI's third-party notices: a Vite plugin that writes dist/third-party-notices.txt
// (served at /ui/third-party-notices.txt, linked from the About dialog) listing every npm
// package the production build contains, with its version, its licence and the licence and
// notice files its package ships, and that fails the build when a package has no licence,
// a licence outside the allow-list, or no licence text to ship.
//
// "Contains" is what the bundler put in the output: the modules of every chunk (the app's,
// the lazily loaded ones such as Swagger UI's and Monaco's, and the web workers', which Vite
// bundles separately: `workerPlugin` goes in `worker.plugins`), and the assets copied from a
// package (Inter's font file, parquet-wasm's WebAssembly). The bundler's own runtime helpers
// (Rolldown's runtime, Vite's preload helper) count as their packages too.

import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";

/**
 * Licences a bundled package may have (SPDX identifiers). An expression passes when it can
 * be satisfied with these alone: `A OR B` needs one of them, `A AND B` both.
 */
export const allowedLicenses = new Set([
  "MIT",
  "ISC",
  "BSD-2-Clause",
  "BSD-3-Clause",
  "Apache-2.0",
  "0BSD",
  "BlueOak-1.0.0",
  "CC0-1.0",
  "Unlicense",
  "Python-2.0",
  // File-level copyleft only: the package's own files stay under the MPL, nothing else is
  // affected. Listed in the build's output when it appears.
  "MPL-2.0",
]);

/**
 * Packages allowed a licence outside the list, each for a reason. The licence must still be
 * exactly this one.
 */
export const licenseExceptions = new Map([
  [
    "@fontsource-variable/inter",
    {
      license: "OFL-1.1",
      reason:
        "a font (Inter), under the SIL Open Font License: free to embed and redistribute with any software, " +
        "its licence travelling with it (also fonts/Inter-LICENSE.txt)",
    },
  ],
]);

/** Licences that carry a copyleft condition of any strength: the build lists the packages that use one. */
const copyleft = /^(MPL|OFL|LGPL|GPL|AGPL|EPL|CDDL|EUPL|CPL|OSL|CECILL)-/;

/** The files of a package that are its licence or notices. */
const licenseFile = /^(licen[cs]e|copying|notice|copyright|third[-_]?party[-_]?notices)([._-].*)?$/i;

/**
 * Whether an SPDX expression can be satisfied with `allowed` licences only. Throws on an
 * expression it can't read.
 */
export function licenseAllowed(expression, allowed = allowedLicenses) {
  const tokens = expression.match(/\(|\)|[^\s()]+/g) ?? [];
  let i = 0;
  const peek = () => tokens[i];
  const next = () => tokens[i++];
  function atom() {
    const token = next();
    if (token === undefined) throw new Error(`incomplete licence expression '${expression}'`);
    if (token === "(") {
      const value = or();
      if (next() !== ")") throw new Error(`unbalanced parentheses in '${expression}'`);
      return value;
    }
    if (token === ")" || /^(AND|OR|WITH)$/i.test(token)) {
      throw new Error(`unexpected '${token}' in '${expression}'`);
    }
    // An exception (`Apache-2.0 WITH LLVM-exception`) only grants more: the licence decides.
    if (/^WITH$/i.test(peek() ?? "")) {
      next();
      if (next() === undefined) throw new Error(`no exception after WITH in '${expression}'`);
    }
    return allowed.has(token.replace(/\+$/, ""));
  }
  function and() {
    let value = atom();
    while (/^AND$/i.test(peek() ?? "")) {
      next();
      value = atom() && value;
    }
    return value;
  }
  function or() {
    let value = and();
    while (/^OR$/i.test(peek() ?? "")) {
      next();
      value = and() || value;
    }
    return value;
  }
  const value = or();
  if (i !== tokens.length) throw new Error(`unexpected '${tokens[i]}' in '${expression}'`);
  return value;
}

/** A package.json's licence as one SPDX expression, or undefined when it declares none. */
export function declaredLicense(pkg) {
  if (typeof pkg.license === "string" && pkg.license.trim() !== "") return pkg.license.trim();
  if (pkg.license && typeof pkg.license.type === "string") return pkg.license.type;
  // The old `licenses` array: the package may be used under any one of them.
  if (Array.isArray(pkg.licenses) && pkg.licenses.length > 0) {
    const types = pkg.licenses.map((l) => (typeof l === "string" ? l : l?.type)).filter(Boolean);
    if (types.length > 0) return types.length === 1 ? types[0] : `(${types.join(" OR ")})`;
  }
  return undefined;
}

/**
 * The package directory a module id or file name belongs to, or undefined for the app's own
 * code. Ids may carry a \0 prefix and a ?query; the bundler's virtual helpers map to the
 * package that writes them.
 */
export function packageDirOf(id, root) {
  let file = id
    .replace(/^\0+/, "")
    .replace(/[?#].*$/, "")
    .replaceAll("\\", "/");
  if (/^(vite\/|\/?@vite\/)/.test(file)) file = path.join(root, "node_modules/vite/package.json");
  if (/^rolldown[/:]/.test(file)) file = path.join(root, "node_modules/rolldown/package.json");
  file = file.replaceAll("\\", "/");
  const marker = "/node_modules/";
  const at = file.lastIndexOf(marker);
  if (at < 0) return undefined;
  const rest = file.slice(at + marker.length).split("/");
  const name = rest[0]?.startsWith("@") ? `${rest[0]}/${rest[1]}` : rest[0];
  if (!name) return undefined;
  return `${file.slice(0, at + marker.length)}${name}`;
}

/**
 * The notices for the packages in `found` (directory -> the bundled files from it), or a
 * list of problems. Packages are sorted by name, so the output is the same for the same
 * build.
 */
export function buildNotices(found, { title }) {
  const problems = [];
  const flagged = [];
  const packages = [];
  for (const [dir, files] of found) {
    const manifest = path.join(dir, "package.json");
    if (!existsSync(manifest)) {
      problems.push(`${dir}: bundled, but has no package.json`);
      continue;
    }
    const pkg = JSON.parse(readFileSync(manifest, "utf8"));
    const name = pkg.name ?? path.basename(dir);
    const label = `${name}@${pkg.version ?? "?"}`;
    const license = declaredLicense(pkg);
    if (!license || /^(UNLICENSED|SEE LICEN[CS]E IN)/i.test(license)) {
      problems.push(`${label}: no licence in its package.json (license: ${JSON.stringify(pkg.license)})`);
      continue;
    }
    const exception = licenseExceptions.get(name);
    let allowed;
    try {
      allowed = exception ? license === exception.license : licenseAllowed(license);
    } catch (error) {
      problems.push(`${label}: ${error.message}`);
      continue;
    }
    if (!allowed) {
      problems.push(
        exception
          ? `${label}: licence '${license}', but its exception is for '${exception.license}'`
          : `${label}: licence '${license}' is not in the allow-list (scripts/third-party-notices.mjs)`,
      );
      continue;
    }
    if ((license.match(/[A-Za-z0-9.+-]+/g) ?? []).some((id) => copyleft.test(id)))
      flagged.push(`${label} (${license})`);
    // The package's licence and notice files, and the licence comments a pre-built bundle
    // keeps beside itself (swagger-ui-dist's swagger-ui-es-bundle.js.LICENSE.txt).
    const texts = readdirSync(dir)
      .filter((f) => licenseFile.test(f) && statSync(path.join(dir, f)).isFile())
      .sort();
    const extracted = [...files]
      .map((f) => `${f}.LICENSE.txt`)
      .filter((f) => existsSync(f))
      .map((f) => path.relative(dir, f).replaceAll("\\", "/"))
      .sort();
    const all = [...new Set([...texts, ...extracted])];
    if (all.length === 0) {
      problems.push(`${label}: no LICENSE, COPYING or NOTICE file in its package to ship`);
      continue;
    }
    const repository = typeof pkg.repository === "string" ? pkg.repository : pkg.repository?.url;
    packages.push({
      name,
      version: pkg.version ?? "",
      license,
      source: (pkg.homepage || repository || "").replace(/^git\+/, "").replace(/\.git$/, ""),
      exception: exception?.reason,
      texts: all.map((f) => ({
        file: f,
        text: readFileSync(path.join(dir, f), "utf8").replace(/\r\n/g, "\n").trimEnd(),
      })),
    });
  }
  if (problems.length > 0) return { problems, flagged };
  packages.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : a.version < b.version ? -1 : 1));

  const rule = "=".repeat(80);
  const thin = "-".repeat(80);
  const width = Math.max(...packages.map((p) => `${p.name} ${p.version}`.length));
  const lines = [
    title,
    "",
    "This web UI contains the open-source packages below, each under its own licence. Each",
    "is listed with its version and licence (an SPDX expression), followed by the licence",
    "and notice files its package ships. Hardcharger itself is under the Apache License 2.0.",
    "",
    `${packages.length} packages:`,
    "",
    ...packages.map((p) => `  ${`${p.name} ${p.version}`.padEnd(width)}  ${p.license}`),
    "",
  ];
  for (const p of packages) {
    lines.push(rule, `${p.name} ${p.version}`, `License: ${p.license}`);
    if (p.source) lines.push(`Source: ${p.source}`);
    if (p.exception) lines.push(`Note: ${p.exception}`);
    for (const t of p.texts) lines.push(thin, `${t.file}:`, "", t.text, "");
  }
  lines.push(rule, "");
  return { problems, flagged, text: lines.join("\n"), packages: packages.map((p) => p.name) };
}

/**
 * The plugins: `plugin` for the build (it writes the file), `workerPlugin` for
 * `worker.plugins` (it only records what each worker bundle contains). One call per build.
 */
export function thirdPartyNotices({ root, fileName = "third-party-notices.txt", title }) {
  const found = new Map();
  function record(bundle) {
    for (const output of Object.values(bundle)) {
      const ids =
        output.type === "chunk"
          ? [...(output.moduleIds ?? Object.keys(output.modules ?? {}))]
          : [...(output.originalFileNames ?? [])].map((f) => path.resolve(root, f));
      for (const id of ids) {
        const dir = packageDirOf(id, root);
        if (!dir) continue;
        if (!found.has(dir)) found.set(dir, new Set());
        found.get(dir).add(id.replace(/^\0+/, "").replace(/[?#].*$/, ""));
      }
    }
  }
  const workerPlugin = {
    name: "hardcharger:third-party-notices-worker",
    apply: "build",
    generateBundle(_options, bundle) {
      record(bundle);
    },
  };
  const plugin = {
    name: "hardcharger:third-party-notices",
    apply: "build",
    enforce: "post",
    generateBundle(_options, bundle) {
      record(bundle);
      const result = buildNotices(found, { title: typeof title === "function" ? title() : title });
      if (result.problems.length > 0) {
        this.error(`third-party notices:\n  ${result.problems.join("\n  ")}`);
      }
      if (result.flagged.length > 0) {
        this.warn(`third-party notices: packages under a copyleft licence: ${result.flagged.join(", ")}`);
      }
      this.emitFile({ type: "asset", fileName, source: result.text });
    },
  };
  return { plugin, workerPlugin };
}
