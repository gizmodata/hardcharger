import { duckdb, formatDialect, type DialectOptions } from "sql-formatter";

type Token = { type: string; raw: string; start: number };
export const sqlFormatStyle = "Leading commas, spaces, lowercase identifiers, uppercase keywords";

/** Keep this small formatter optional: incomplete or unsupported SQL stays exactly as supplied. */
export function formatSql(sql: string): { text: string; error?: string } {
  if (!sql.trim()) return { text: sql };
  if (sql.length > 100_000)
    return { text: sql, error: "SQL is too large to format here (100,000 characters maximum)." };
  try {
    const before = formattedTokens(sql);
    const after = formattedTokens(before.text);
    // Formatting must not change literals, quoted names, parameters or comments. In particular,
    // dollar-quoted SQL passed as a function argument is data, never a second script to rewrite.
    if (signature(before.tokens) !== signature(after.tokens)) throw new Error("token changes");
    const edits: { from: number; to: number; text: string }[] = [];
    for (let i = 0; i < after.tokens.length; i++) {
      const comma = after.tokens[i]!;
      if (comma.type !== "COMMA") continue;
      let j = i + 1;
      while (after.tokens[j]?.type.endsWith("COMMENT")) j++;
      const next = after.tokens[j];
      if (!next || next.type === "EOF") continue;
      const line = before.text.lastIndexOf("\n", next.start - 1) + 1;
      const indent = before.text.slice(line, next.start);
      // Move only multiline list separators. Comments stay with their original expression;
      // commas inside strings/comments never appear as COMMA tokens.
      if (line <= comma.start || !/^ +$/.test(indent)) continue;
      edits.push({ from: comma.start, to: comma.start + 1, text: "" });
      edits.push({ from: next.start - Math.min(2, indent.length), to: next.start, text: ", " });
    }
    let text = before.text;
    for (const edit of edits.sort((a, b) => b.from - a.from))
      text = text.slice(0, edit.from) + edit.text + text.slice(edit.to);
    return { text };
  } catch {
    return { text: sql, error: "This SQL could not be formatted safely. Showing the original text." };
  }
}

function formattedTokens(sql: string): { text: string; tokens: Token[] } {
  let tokens: Token[] = [];
  const dialect: DialectOptions = {
    ...duckdb,
    tokenizerOptions: {
      ...duckdb.tokenizerOptions,
      reservedKeywords: [...duckdb.tokenizerOptions.reservedKeywords, "EXPORT_STATE"],
      // The dialect omits named $binds because they overlap $tag$ strings. Match
      // a complete name only when no closing $ follows; strings remain literal.
      paramTypes: {
        ...duckdb.tokenizerOptions.paramTypes,
        custom: [{ regex: "\\$[\\p{L}_][\\p{L}\\p{N}_]*(?![\\p{L}\\p{N}_$])" }],
      },
      postProcess: (found) => {
        tokens = found;
        return found;
      },
    },
  };
  const text = formatDialect(sql, {
    dialect,
    tabWidth: 4,
    useTabs: false,
    keywordCase: "upper",
    dataTypeCase: "upper",
    functionCase: "lower",
    identifierCase: "lower",
    linesBetweenQueries: 1,
  });
  return { text, tokens };
}

function signature(tokens: Token[]): string {
  return JSON.stringify(
    tokens
      .filter((t) => t.type !== "EOF")
      .map((t) =>
        t.type === "IDENTIFIER" ||
        t.type.startsWith("RESERVED_") ||
        ["CASE", "END", "WHEN", "ELSE", "THEN", "LIMIT", "BETWEEN", "AND", "OR", "XOR"].includes(t.type)
          ? t.raw.replace(/[A-Z]/g, (letter) => letter.toLowerCase()).replace(/\s+/g, " ")
          : t.raw,
      ),
  );
}
