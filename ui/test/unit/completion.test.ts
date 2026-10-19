import { describe, expect, it } from "vitest";

import { searchSql } from "../../src/lib/catalogCache";
import { completionContext, referencedTables, sqlIdentifier } from "../../src/lib/completionContext";

const at = (sql: string) => completionContext(sql, sql);

describe("completionContext", () => {
  it("finds the dotted name being typed", () => {
    expect(at("SELECT * FROM shop.main.or")).toMatchObject({ qualifiers: ["shop", "main"], partial: "or" });
    expect(at("SELECT o.")).toMatchObject({ qualifiers: ["o"], partial: "" });
    expect(at('SELECT * FROM "My Cat".')).toMatchObject({ qualifiers: ["My Cat"], partial: "" });
    expect(at('SELECT "Mix')).toMatchObject({ qualifiers: [], partial: "Mix" });
  });

  it("knows where a table name goes", () => {
    for (const sql of [
      "SELECT * FROM ",
      "SELECT * FROM a JOIN ",
      "INSERT INTO ",
      "UPDATE ",
      "DESCRIBE ",
      "SELECT * FROM a, ",
    ]) {
      expect(at(sql).tableContext, sql).toBe(true);
    }
    for (const sql of ["SELECT ", "SELECT a, ", "SELECT * FROM t WHERE ", "SELECT * FROM t ORDER BY "]) {
      expect(at(sql).tableContext, sql).toBe(false);
    }
  });

  it("offers nothing inside a string or a comment", () => {
    expect(at("SELECT 'FROM ").suppressed).toBe(true);
    expect(at("SELECT 1 -- FROM ").suppressed).toBe(true);
    expect(at("SELECT /* FROM ").suppressed).toBe(true);
    expect(at("SELECT 'done' FROM ").suppressed).toBe(false);
  });

  it("reads the tables a statement references, with their aliases", () => {
    const refs = referencedTables("SELECT * FROM shop.main.orders o JOIN customers AS c ON 1=1 WHERE x");
    expect(refs.get("o")).toEqual({ catalog: "shop", schema: "main", table: "orders" });
    expect(refs.get("orders")).toEqual({ catalog: "shop", schema: "main", table: "orders" });
    expect(refs.get("c")).toEqual({ table: "customers" });
    expect(referencedTables('SELECT * FROM a, "Odd T" t').get("t")).toEqual({ table: "Odd T" });
    expect(refs.has("where")).toBe(false);
  });
});

describe("sqlIdentifier", () => {
  it("quotes what needs it: mixed case, spaces, quotes, keywords", () => {
    const keywords = new Set(["ORDER", "SELECT"]);
    expect(sqlIdentifier("orders", keywords)).toBe("orders");
    expect(sqlIdentifier("order", keywords)).toBe('"order"');
    expect(sqlIdentifier("Mixed Case", keywords)).toBe('"Mixed Case"');
    expect(sqlIdentifier('say "hi"', keywords)).toBe('"say ""hi"""');
    expect(sqlIdentifier("2fast", keywords)).toBe('"2fast"');
  });
});

describe("searchSql", () => {
  it("escapes LIKE wildcards and quotes in the term", () => {
    const sql = searchSql("a_b%c'd");
    expect(sql).toContain("ILIKE '%a\\_b\\%c''d%' ESCAPE '\\'");
    expect(sql).toContain("LIMIT 101");
  });
});
