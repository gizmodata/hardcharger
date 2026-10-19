import { describe, expect, it } from "vitest";

import {
  exactLike,
  firstKeyword,
  isTransactionStatement,
  mayChangeCatalog,
  quoteIdentifier,
  quoteLiteral,
  splitStatements,
  statementAt,
  statementPreview,
} from "../../src/lib/sql";

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

describe("firstKeyword", () => {
  it("is the statement's first word, upper-cased, after comments", () => {
    expect(firstKeyword("  select 1")).toBe("SELECT");
    expect(firstKeyword("-- note\n/* a /* nested */ one */ begin transaction")).toBe("BEGIN");
    expect(firstKeyword("(SELECT 1)")).toBe("");
    expect(firstKeyword("-- only a comment")).toBe("");
  });
});

describe("mayChangeCatalog and isTransactionStatement", () => {
  it("knows the statements that create, drop or attach something", () => {
    expect(mayChangeCatalog("SELECT 1; CREATE TABLE t (a INT)")).toBe(true);
    expect(mayChangeCatalog("attach ':memory:' AS x")).toBe(true);
    expect(mayChangeCatalog("SELECT 'CREATE TABLE'")).toBe(false);
    expect(mayChangeCatalog("-- DROP\nSELECT 1")).toBe(false);
  });
  it("knows the statements that begin or end a transaction", () => {
    for (const sql of ["BEGIN", "start transaction", "COMMIT", "END", "ROLLBACK", "ABORT"]) {
      expect(isTransactionStatement(sql)).toBe(true);
    }
    expect(isTransactionStatement("SELECT 'BEGIN'")).toBe(false);
  });
});

describe("statementPreview", () => {
  it("squeezes whitespace and shortens", () => {
    expect(statementPreview("SELECT\n  a,\n  b  FROM t")).toBe("SELECT a, b FROM t");
    expect(statementPreview("x".repeat(50), 10)).toBe(`${"x".repeat(10)}…`);
  });
});

describe("statementAt: which statement Run executes", () => {
  const sql = "SELECT 1;\nSELECT 2;\n\nSELECT 3";
  const at = (needle: string, after = 0) => statementAt(sql, sql.indexOf(needle) + after)?.text;
  it("is the statement the cursor is in", () => {
    expect(at("SELECT 2", 3)).toBe("SELECT 2");
    expect(at("SELECT 3", 8)).toBe("SELECT 3");
  });
  it("right after a terminator, still the statement it ended", () => {
    expect(at(";", 1)).toBe("SELECT 1");
  });
  it("on blank lines between statements, the one before; at the top, the first", () => {
    expect(statementAt(sql, sql.indexOf("\n\n") + 1)?.text).toBe("SELECT 2");
    expect(statementAt(sql, 0)?.text).toBe("SELECT 1");
  });
});
