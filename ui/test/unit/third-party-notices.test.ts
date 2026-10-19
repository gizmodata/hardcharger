// @vitest-environment node
// The build's third-party notices (scripts/third-party-notices.mjs): which licences pass the
// allow-list, which module ids belong to which package, and what the notices file holds.
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { buildNotices, declaredLicense, licenseAllowed, packageDirOf } from "../../scripts/third-party-notices.mjs";

describe("the licence allow-list", () => {
  it("passes permissive licences and expressions satisfiable with them", () => {
    for (const ok of [
      "MIT",
      "Apache-2.0",
      "0BSD",
      "BSD-3-Clause",
      "MIT OR Apache-2.0",
      "(MIT OR GPL-3.0-only)",
      "MIT AND ISC",
      "Apache-2.0 WITH LLVM-exception",
      "(MIT AND (BSD-2-Clause OR GPL-2.0-only))",
      "MPL-2.0",
    ]) {
      expect(licenseAllowed(ok), ok).toBe(true);
    }
  });

  it("refuses copyleft and unknown licences, and AND with one of them", () => {
    for (const bad of [
      "GPL-3.0-only",
      "LGPL-2.1-or-later",
      "AGPL-3.0-only",
      "MIT AND GPL-2.0-only",
      "SSPL-1.0",
      "OFL-1.1",
      "Commercial",
    ]) {
      expect(licenseAllowed(bad), bad).toBe(false);
    }
  });

  it("throws on an expression it can't read", () => {
    expect(() => licenseAllowed("MIT OR")).toThrow();
    expect(() => licenseAllowed("(MIT")).toThrow();
    expect(() => licenseAllowed("MIT Apache-2.0")).toThrow();
  });

  it("reads the package.json forms", () => {
    expect(declaredLicense({ license: "MIT" })).toBe("MIT");
    expect(declaredLicense({ license: { type: "ISC" } })).toBe("ISC");
    expect(declaredLicense({ licenses: [{ type: "MIT" }, { type: "Apache-2.0" }] })).toBe("(MIT OR Apache-2.0)");
    expect(declaredLicense({})).toBeUndefined();
  });
});

describe("module ids to packages", () => {
  const root = "/app/ui";
  it("finds the package, scoped or not, nested or not", () => {
    expect(packageDirOf("/app/ui/node_modules/react/index.js", root)).toBe("/app/ui/node_modules/react");
    expect(packageDirOf("/app/ui/node_modules/@scope/pkg/dist/a.js?worker", root)).toBe(
      "/app/ui/node_modules/@scope/pkg",
    );
    expect(packageDirOf("/app/ui/node_modules/a/node_modules/b/x.js", root)).toBe(
      "/app/ui/node_modules/a/node_modules/b",
    );
    expect(packageDirOf("\0/app/ui/node_modules/tslib/tslib.es6.mjs?commonjs-proxy", root)).toBe(
      "/app/ui/node_modules/tslib",
    );
  });
  it("leaves the app's own code out, and maps the bundler's helpers to their package", () => {
    expect(packageDirOf("/app/ui/src/main.tsx", root)).toBeUndefined();
    expect(packageDirOf("\0vite/preload-helper.js", root)).toBe("/app/ui/node_modules/vite");
    expect(packageDirOf("\0rolldown/runtime.js", root)).toBe("/app/ui/node_modules/rolldown");
  });
});

describe("the notices file", () => {
  let dir = "";
  afterEach(() => {
    if (dir) rmSync(dir, { recursive: true, force: true });
    dir = "";
  });
  function fakePackage(name: string, pkg: Record<string, unknown>, files: Record<string, string>): string {
    const at = path.join(dir, "node_modules", name);
    mkdirSync(at, { recursive: true });
    writeFileSync(path.join(at, "package.json"), JSON.stringify({ name, version: "1.2.3", ...pkg }));
    for (const [f, text] of Object.entries(files)) writeFileSync(path.join(at, f), text);
    return at;
  }

  it("lists each package with its licence and every licence and notice file, sorted", () => {
    dir = mkdtempSync(path.join(os.tmpdir(), "tpn-"));
    const b = fakePackage(
      "b-pkg",
      { license: "Apache-2.0" },
      { LICENSE: "Apache text", NOTICE: "B notice", "x.js": "" },
    );
    const a = fakePackage("a-pkg", { license: "MIT" }, { "LICENSE.md": "MIT text\r\n", "bundle.js": "" });
    writeFileSync(path.join(a, "bundle.js.LICENSE.txt"), "/*! bundled dep, MIT */");
    const result = buildNotices(
      new Map([
        [b, new Set([path.join(b, "x.js")])],
        [a, new Set([path.join(a, "bundle.js")])],
      ]),
      { title: "Test notices" },
    );
    expect(result.problems).toEqual([]);
    expect(result.packages).toEqual(["a-pkg", "b-pkg"]);
    const text = result.text ?? "";
    expect(text.startsWith("Test notices\n")).toBe(true);
    expect(text).toMatch(/^a-pkg 1\.2\.3\nLicense: MIT$/m);
    expect(text).toContain("LICENSE.md:\n\nMIT text\n");
    expect(text).toContain("bundle.js.LICENSE.txt:\n\n/*! bundled dep, MIT */");
    expect(text).toContain("NOTICE:\n\nB notice");
    expect(text.indexOf("a-pkg 1.2.3\nLicense")).toBeLessThan(text.indexOf("b-pkg 1.2.3\nLicense"));
  });

  it("fails on a disallowed licence, a missing licence and a package with no licence file", () => {
    dir = mkdtempSync(path.join(os.tmpdir(), "tpn-"));
    const gpl = fakePackage("gpl-pkg", { license: "GPL-3.0-only" }, { LICENSE: "GPL" });
    const none = fakePackage("none-pkg", {}, { LICENSE: "?" });
    const bare = fakePackage("bare-pkg", { license: "MIT" }, {});
    const font = fakePackage("@fontsource-variable/inter", { license: "MIT" }, { LICENSE: "x" });
    const result = buildNotices(
      new Map([
        [gpl, new Set<string>()],
        [none, new Set<string>()],
        [bare, new Set<string>()],
        [font, new Set<string>()],
      ]),
      { title: "t" },
    );
    expect(result.text).toBeUndefined();
    expect(result.problems).toEqual([
      "gpl-pkg@1.2.3: licence 'GPL-3.0-only' is not in the allow-list (scripts/third-party-notices.mjs)",
      "none-pkg@1.2.3: no licence in its package.json (license: undefined)",
      "bare-pkg@1.2.3: no LICENSE, COPYING or NOTICE file in its package to ship",
      // An exception names its licence exactly.
      "@fontsource-variable/inter@1.2.3: licence 'MIT', but its exception is for 'OFL-1.1'",
    ]);
  });
});
