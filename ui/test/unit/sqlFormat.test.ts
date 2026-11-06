import { describe, expect, it } from "vitest";
import { formatSql } from "../../src/lib/sqlFormat";

describe("SQL formatting", () => {
  it("uses spaced leading commas, lowercase unquoted names and uppercase keywords", () => {
    expect(formatSql("select Foo, BAR as B from MyTable where Foo > 0")).toEqual({
      text: "SELECT\n    foo\n  , bar AS b\nFROM\n    mytable\nWHERE\n    foo > 0",
    });
  });
  it("preserves multiline literals, quoted identifiers and comments while moving real commas", () => {
    const result = formatSql("select Foo, -- Keep CASE,\n'Line,\nTwo' as \"MiX\", E'escaped\\'quote' as S from T");
    expect(result.error).toBeUndefined();
    expect(result.text).toContain("foo -- Keep CASE,");
    expect(result.text).toContain("  , 'Line,\nTwo' AS \"MiX\"");
    expect(result.text).toContain("E'escaped\\'quote'");
  });
  it("preserves dollar-quoted fragment SQL and recognizes EXPORT_STATE", () => {
    const result = formatSql("select count(*) export_state as STATE, $q$SELECT Foo, BAR FROM T$q$ as Q from MyTable");
    expect(result.error).toBeUndefined();
    expect(result.text).toContain("count(*) EXPORT_STATE AS state");
    expect(result.text).toContain("$q$SELECT Foo, BAR FROM T$q$");
  });
  it("formats named binds without consuming dollar-quoted SQL or changing bind names", () => {
    const sql =
      "select count_star() export_state as S, $Name, $1, ?, $q$SELECT Foo, BAR$q$, $$Keep CASE$$ from read_parquet($hc_file_1) where filename in ($hc_file_2, $hc_file_3)";
    const result = formatSql(sql);
    expect(result.error).toBeUndefined();
    for (const value of [
      "$Name",
      "$1",
      "?",
      "$q$SELECT Foo, BAR$q$",
      "$$Keep CASE$$",
      "$hc_file_1",
      "$hc_file_2",
      "$hc_file_3",
    ])
      expect(result.text).toContain(value);
    expect(result.text).toContain("count_star() EXPORT_STATE AS s");
    expect(formatSql(result.text)).toEqual(result);
  });
  it("keeps inline argument lists and formats joins, CTEs and multiple statements idempotently", () => {
    const sql =
      "with Foo as (select A, B from T) select F.A, coalesce(F.B, 0) as X from Foo F join Other O on F.A = O.A; select 2, 3";
    const result = formatSql(sql);
    expect(result.error).toBeUndefined();
    expect(result.text).toContain("coalesce(f.b, 0)");
    expect(result.text).toContain("JOIN");
    expect(formatSql(result.text)).toEqual(result);
  });
  it("does not silently fold non-ASCII identifiers", () => {
    const sql = 'select Äpfel, "MiX" from T';
    const result = formatSql(sql);
    expect(result.text).toContain("Äpfel");
    expect(result.text).toContain('"MiX"');
  });
  it("leaves incomplete, truncated, and oversized input intact", () => {
    for (const sql of ["SELECT 'unfinished", "SELECT (", "SELECT …", "x".repeat(100_001)]) {
      expect(formatSql(sql).text).toBe(sql);
      expect(formatSql(sql).error).toBeTruthy();
    }
    expect(formatSql(" \n")).toEqual({ text: " \n" });
  });
  it("preserves parameters and nested comments or falls back without changing text", () => {
    for (const sql of ['select "Case", $Name, $1, ? from T', "select A /* nested /* Keep */ CASE */ , B from T"]) {
      const result = formatSql(sql);
      if (result.error) expect(result.text).toBe(sql);
      else {
        expect(result.text).toContain(sql.includes("$Name") ? "$Name" : "/* nested /* Keep */ CASE */");
      }
    }
  });
});
