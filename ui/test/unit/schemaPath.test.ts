import { describe, expect, it } from "vitest";

import { parseSchemaName, schemaSql, schemaTree, spellSchemaPath } from "../../src/lib/schemaPath";
import { sqlIdentifier } from "../../src/lib/completionContext";

const ident = (name: string) => sqlIdentifier(name, new Set());

describe("parseSchemaName", () => {
  it("reads a plain name, a nested path, and quoted parts with dots and quotes in them", () => {
    expect(parseSchemaName("main")).toEqual(["main"]);
    expect(parseSchemaName("sales.payroll")).toEqual(["sales", "payroll"]);
    expect(parseSchemaName('"sales.payroll"')).toEqual(["sales.payroll"]);
    expect(parseSchemaName('sales."pay roll".eu')).toEqual(["sales", "pay roll", "eu"]);
    expect(parseSchemaName('"a""b".c')).toEqual(['a"b', "c"]);
    expect(parseSchemaName("")).toEqual([""]);
  });
});

describe("schemaSql", () => {
  it("writes the path back as SQL, quoting what SQL needs quoted", () => {
    expect(schemaSql(["sales", "payroll"], ident)).toBe("sales.payroll");
    expect(schemaSql(["sales.payroll"], ident)).toBe('"sales.payroll"');
    expect(schemaSql(["Mixed Case", "x"], ident)).toBe('"Mixed Case".x');
    for (const spelled of ["main", "sales.payroll", '"sales.payroll"', 'a."b.c".d']) {
      expect(schemaSql(parseSchemaName(spelled), ident)).toBe(spelled);
    }
    // The metadata spells a part bare unless it holds a dot or a quote: SQL still quotes it.
    expect(parseSchemaName("My Schema")).toEqual(["My Schema"]);
    expect(schemaSql(parseSchemaName("My Schema"), ident)).toBe('"My Schema"');
    expect(schemaSql(parseSchemaName("sel.Odd Name"), ident)).toBe('sel."Odd Name"');
  });
});

describe("spellSchemaPath", () => {
  it("spells a path as the metadata does: dots between parts, quotes only around a dot or a quote", () => {
    expect(spellSchemaPath(["sales", "payroll"])).toBe("sales.payroll");
    expect(spellSchemaPath(["sales.payroll"])).toBe('"sales.payroll"');
    expect(spellSchemaPath(["My Schema"])).toBe("My Schema");
    expect(spellSchemaPath(['say "hi"', "x"])).toBe('"say ""hi""".x');
    for (const path of [["a"], ["a", "b.c", "d"], ["My Schema", "x"]])
      expect(parseSchemaName(spellSchemaPath(path))).toEqual(path);
  });
});

describe("schemaTree", () => {
  it("nests schemas under their parents, keeping the listing's order and each one's spelling", () => {
    const tree = schemaTree([
      "main",
      "sales",
      "sales.payroll",
      "sales.payroll.eu",
      '"sales.payroll"',
      "hr",
      "hr.payroll",
    ]);
    expect(tree.map((n) => n.name)).toEqual(["main", "sales", "sales.payroll", "hr"]);
    const sales = tree[1]!;
    expect(sales.listed).toBe(true);
    expect(sales.children.map((n) => n.spelled)).toEqual(["sales.payroll"]);
    expect(sales.children[0]!.children.map((n) => [n.name, n.spelled, n.path])).toEqual([
      ["eu", "sales.payroll.eu", ["sales", "payroll", "eu"]],
    ]);
    // The quoted top-level twin is its own root, not sales' child.
    expect(tree[2]!.path).toEqual(["sales.payroll"]);
    expect(tree[3]!.children.map((n) => n.path)).toEqual([["hr", "payroll"]]);
  });

  it("makes a placeholder for a parent the listing lacks, and fills it in when it comes", () => {
    const tree = schemaTree(["sales.payroll.eu", "other"]);
    expect(tree.map((n) => [n.name, n.listed])).toEqual([
      ["sales", false],
      ["other", true],
    ]);
    expect(tree[0]!.children[0]!.listed).toBe(false);
    expect(tree[0]!.children[0]!.spelled).toBe("sales.payroll");
    expect(tree[0]!.children[0]!.children[0]!.listed).toBe(true);
    const later = schemaTree(["sales.payroll.eu", "sales"]);
    expect(later[0]!.listed).toBe(true);
    expect(later).toHaveLength(1);
  });
});
