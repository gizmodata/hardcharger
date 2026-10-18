import { describe, expect, it } from "vitest";

import { exactLike, quoteIdentifier, quoteLiteral, splitStatements, statementAt } from "../../src/lib/sql";

describe("splitStatements", () => {
  it("splits on semicolons and drops empty statements", () => {
    expect(splitStatements("SELECT 1; SELECT 2;;\n  ").map((s) => s.text)).toEqual(["SELECT 1", "SELECT 2"]);
  });

  it("ignores semicolons in strings, identifiers, comments and dollar quotes", () => {
    const sql = [
      "SELECT 'a;b', \"c;d\" FROM t -- not; here",
      "; SELECT /* one; /* nested; */ still; */ 2",
      "; SELECT $$x;y$$, $tag$ ; $tag$",
      "; SELECT E'it\\'s; fine'",
      "; SELECT 'it''s; fine'",
    ].join("\n");
    // A comment after a statement's code is not part of it.
    expect(splitStatements(sql).map((s) => s.text)).toEqual([
      "SELECT 'a;b', \"c;d\" FROM t",
      "SELECT /* one; /* nested; */ still; */ 2",
      "SELECT $$x;y$$, $tag$ ; $tag$",
      "SELECT E'it\\'s; fine'",
      "SELECT 'it''s; fine'",
    ]);
  });

  it("keeps $1 parameters as code", () => {
    expect(splitStatements("SELECT $1; SELECT $2").map((s) => s.text)).toEqual(["SELECT $1", "SELECT $2"]);
  });

  it("skips statements made only of comments", () => {
    expect(splitStatements("-- just a comment\n; /* and */ ; SELECT 3").map((s) => s.text)).toEqual(["SELECT 3"]);
  });

  it("reports offsets into the text", () => {
    const sql = "  SELECT 1;\nSELECT 2";
    const [a, b] = splitStatements(sql);
    expect(sql.slice(a!.start, a!.end)).toBe("SELECT 1");
    expect(sql.slice(b!.start, b!.end)).toBe("SELECT 2");
  });
});

describe("statementAt", () => {
  const sql = "SELECT 1;\n\n  SELECT 2;\nSELECT 3";
  const at = (offset: number) => statementAt(sql, offset)?.text;

  it("finds the statement the cursor is in", () => {
    expect(at(3)).toBe("SELECT 1");
    expect(at(sql.indexOf("SELECT 2") + 4)).toBe("SELECT 2");
    expect(at(sql.length)).toBe("SELECT 3");
  });

  it("gives a cursor just after a semicolon to the statement before it", () => {
    expect(at(sql.indexOf(";") + 1)).toBe("SELECT 1");
  });

  it("gives blank lines between statements to the one before, and a line's start to the one on it", () => {
    expect(at(sql.indexOf("\n\n") + 1)).toBe("SELECT 1");
    expect(at(sql.indexOf("  SELECT 2"))).toBe("SELECT 2");
  });

  it("works at the very top, and on empty text", () => {
    expect(statementAt("\n\nSELECT 9", 0)?.text).toBe("SELECT 9");
    expect(statementAt("", 0)).toBeUndefined();
    expect(statementAt("-- nothing", 3)).toBeUndefined();
  });
});

describe("quoting", () => {
  it("quotes identifiers and literals", () => {
    expect(quoteIdentifier('we"ird')).toBe('"we""ird"');
    expect(quoteLiteral("it's")).toBe("'it''s'");
  });

  it("escapes LIKE metacharacters for exact metadata filters", () => {
    expect(exactLike("my_table%\\x")).toBe("my\\_table\\%\\\\x");
  });
});
