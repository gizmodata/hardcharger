// The app's stylesheets are global, and so are the editor's (Monaco's) elements: a rule for
// a generic class name the editor also uses styles the editor too. The app's `.main`
// (flex-direction: column) once matched the suggestion list's `.main` row and pushed every
// label out of its row, so the list showed only icons. Swagger UI's stylesheet, full of such
// names, is kept in the API page's shadow root and must never be imported as a global one.
import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

const root = path.resolve(import.meta.dirname, "../..");

function files(dir: string, ext: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) files(full, ext, out);
    else if (full.endsWith(ext)) out.push(full);
  }
  return out;
}

/** Every selector of a stylesheet's style rules (not at-rule preludes or keyframe steps). */
function selectors(css: string): string[] {
  const out: string[] = [];
  for (const m of css.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/([^{};]+)\{/g)) {
    const prelude = m[1]!.trim();
    if (prelude.startsWith("@") || /^(from|to|\d+(\.\d+)?%)\b/.test(prelude)) continue;
    out.push(...prelude.split(",").map((s) => s.trim()));
  }
  return out;
}

const classes = (selector: string) => [...selector.matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)].map((m) => m[1]!);

describe("stylesheet isolation", () => {
  const monaco = new Set(
    files(path.join(root, "node_modules/monaco-editor/esm"), ".css").flatMap((f) =>
      selectors(readFileSync(f, "utf8")).flatMap(classes),
    ),
  );
  // The global stylesheets: everything in src/styles but what goes into a shadow root.
  const global = files(path.join(root, "src/styles"), ".css").filter((f) => !f.endsWith("swagger-shadow.css"));

  it("finds the editor's class names", () => {
    expect(monaco.size).toBeGreaterThan(300);
    for (const c of ["main", "tree", "message", "contents", "left", "right"]) expect(monaco).toContain(c);
  });

  it("no global rule names a class the editor uses, unless it is a rule for the editor", () => {
    const clashes: string[] = [];
    for (const f of global) {
      for (const s of selectors(readFileSync(f, "utf8"))) {
        // A rule that names an editor class on purpose (an override) says so with monaco-*.
        if (/\.(monaco-|codicon)/.test(s)) continue;
        const shared = classes(s).filter((c) => monaco.has(c));
        if (shared.length) clashes.push(`${path.basename(f)}: ${s} (${shared.join(", ")})`);
      }
    }
    expect(clashes).toEqual([]);
  });

  it("the guard catches a clash", () => {
    const clash = selectors(".main { flex-direction: column }\n.app-main > .page { flex: 1 }").filter((s) =>
      classes(s).some((c) => monaco.has(c)),
    );
    expect(clash).toEqual([".main"]);
  });

  it("Swagger UI's stylesheet is only ever imported inline (for the shadow root)", () => {
    const imports = files(path.join(root, "src"), ".tsx")
      .concat(files(path.join(root, "src"), ".ts"))
      .flatMap((f) =>
        [
          ...readFileSync(f, "utf8").matchAll(
            /import\s+(?:[\w{}\s,]+\s+from\s+)?["']([^"']*swagger[^"']*\.css[^"']*)["']/g,
          ),
        ].map((m) => `${path.basename(f)}: ${m[1]}`),
      );
    expect(imports.length).toBeGreaterThan(0);
    for (const i of imports) expect(i, i).toMatch(/\?inline$/);
  });
});
