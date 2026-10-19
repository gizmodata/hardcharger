/**
 * What the SQL editor's autocomplete offers at the cursor: a light parse of
 * the current statement, not a SQL grammar. It finds the dotted name being typed (`a.b.c|` →
 * qualifiers [a, b], partial c), whether the cursor is where a table name goes (after FROM,
 * JOIN, INTO, UPDATE, TABLE, DESCRIBE), and the tables the statement already references with
 * their aliases (`FROM sales.orders o` → o, orders), so `o.` can offer orders' columns.
 *
 * Identifiers follow DuckDB: unquoted ones are case-insensitive, "quoted" ones keep their case
 * and may contain anything ("" escapes a quote).
 */

export interface TableRef {
  catalog?: string;
  schema?: string;
  table: string;
}

export interface CompletionContext {
  /** Inside a string literal or comment: offer nothing. */
  suppressed: boolean;
  /** Dotted names before the partial word (`cat.sch.` → ['cat', 'sch']). */
  qualifiers: string[];
  /** The partial word typed so far (without a leading quote). */
  partial: string;
  /** Where the partial (including any opening quote) starts in the text before the cursor. */
  partialStart: number;
  /** The cursor is where a table name goes. */
  tableContext: boolean;
  /** Tables the statement references, by alias and by name (lower-cased keys). */
  aliases: Map<string, TableRef>;
}

const TABLE_KEYWORDS = new Set(["FROM", "JOIN", "INTO", "UPDATE", "TABLE", "DESCRIBE"]);
// Keywords that end a table-reference context (and can't be aliases).
const CLAUSE_KEYWORDS = new Set([
  "SELECT",
  "WHERE",
  "ON",
  "USING",
  "GROUP",
  "ORDER",
  "HAVING",
  "LIMIT",
  "OFFSET",
  "SET",
  "VALUES",
  "UNION",
  "INTERSECT",
  "EXCEPT",
  "WINDOW",
  "QUALIFY",
  "RETURNING",
  "AND",
  "OR",
  "NOT",
  "AS",
  "BY",
  "LEFT",
  "RIGHT",
  "INNER",
  "OUTER",
  "FULL",
  "CROSS",
  "NATURAL",
  "POSITIONAL",
  "ASOF",
  "ANTI",
  "SEMI",
  "LATERAL",
  "JOIN",
  "FROM",
  "WITH",
  "CASE",
  "WHEN",
  "THEN",
  "ELSE",
  "END",
  "IS",
  "NULL",
  "IN",
  "EXISTS",
  "BETWEEN",
  "LIKE",
  "ILIKE",
  "DESCRIBE",
  "TABLE",
  "INTO",
  "UPDATE",
  "DELETE",
  "INSERT",
  "CREATE",
  "DROP",
  "ALTER",
  "SAMPLE",
  "TABLESAMPLE",
  "PIVOT",
  "UNPIVOT",
  "DISTINCT",
  "ALL",
  "FILTER",
  "OVER",
]);

interface Token {
  kind: "ident" | "quoted" | "dot" | "comma" | "other";
  text: string; // identifier value (unquoted) or the raw char
  start: number;
}

/** Tokens of `sql`, skipping strings and comments; `openState` says whether `sql` ENDS inside one. */
function tokenize(sql: string): { tokens: Token[]; endsInLiteral: boolean } {
  const tokens: Token[] = [];
  let i = 0;
  const n = sql.length;
  while (i < n) {
    const ch = sql[i]!;
    const next = sql[i + 1];
    if (ch === "'") {
      const close = sql.indexOf("'", i + 1);
      if (close < 0) return { tokens, endsInLiteral: true };
      i = close + 1;
      // '' inside a string is an escaped quote: just keep scanning (the next ' re-opens).
      continue;
    }
    if (ch === "-" && next === "-") {
      const nl = sql.indexOf("\n", i);
      if (nl < 0) return { tokens, endsInLiteral: true };
      i = nl + 1;
      continue;
    }
    if (ch === "/" && next === "*") {
      const close = sql.indexOf("*/", i + 2);
      if (close < 0) return { tokens, endsInLiteral: true };
      i = close + 2;
      continue;
    }
    if (ch === '"') {
      let j = i + 1;
      let value = "";
      for (;;) {
        if (j >= n) {
          // an unterminated quoted identifier: the partial being typed
          tokens.push({ kind: "quoted", text: value, start: i });
          return { tokens, endsInLiteral: false };
        }
        if (sql[j] === '"') {
          if (sql[j + 1] === '"') {
            value += '"';
            j += 2;
            continue;
          }
          break;
        }
        value += sql[j++]!;
      }
      tokens.push({ kind: "quoted", text: value, start: i });
      i = j + 1;
      continue;
    }
    if (/[A-Za-z_]/.test(ch)) {
      let j = i + 1;
      while (j < n && /[A-Za-z0-9_$]/.test(sql[j]!)) j++;
      tokens.push({ kind: "ident", text: sql.slice(i, j), start: i });
      i = j;
      continue;
    }
    if (ch === ".") tokens.push({ kind: "dot", text: ".", start: i });
    else if (ch === ",") tokens.push({ kind: "comma", text: ",", start: i });
    else if (!/\s/.test(ch)) tokens.push({ kind: "other", text: ch, start: i });
    i++;
  }
  return { tokens, endsInLiteral: false };
}

const isName = (t: Token | undefined) =>
  !!t && (t.kind === "quoted" || (t.kind === "ident" && !CLAUSE_KEYWORDS.has(t.text.toUpperCase())));
const keyOf = (t: Token) => (t.kind === "quoted" ? t.text : t.text.toLowerCase());

/** Tables referenced after FROM / JOIN / UPDATE / INTO (and in comma lists after FROM). */
export function referencedTables(statement: string): Map<string, TableRef> {
  const { tokens } = tokenize(statement);
  const aliases = new Map<string, TableRef>();
  let inFromList = false;
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i]!;
    const upper = t.kind === "ident" ? t.text.toUpperCase() : "";
    const startsRef = TABLE_KEYWORDS.has(upper) || (inFromList && t.kind === "comma");
    if (upper === "FROM" || upper === "JOIN") inFromList = true;
    else if (CLAUSE_KEYWORDS.has(upper) && upper !== "AS") inFromList = upper === "FROM" || upper === "JOIN";
    if (!startsRef) continue;
    // A dotted name: up to three parts.
    const parts: Token[] = [];
    let j = i + 1;
    let trailingDot = false;
    while (isName(tokens[j]) && parts.length < 3) {
      parts.push(tokens[j]!);
      trailingDot = tokens[j + 1]?.kind === "dot";
      if (trailingDot) j += 2;
      else {
        j++;
        break;
      }
    }
    // `FROM shop.` is a name still being typed, not a table called shop.
    if (parts.length === 0 || trailingDot) continue;
    const names = parts.map((p) => p.text) as [string, ...string[]];
    const ref: TableRef =
      parts.length === 3
        ? { catalog: names[0], schema: names[1]!, table: names[2]! }
        : parts.length === 2
          ? { schema: names[0], table: names[1]! }
          : { table: names[0] };
    aliases.set(keyOf(parts[parts.length - 1]!), ref);
    let k = j;
    if (tokens[k]?.kind === "ident" && tokens[k]!.text.toUpperCase() === "AS") k++;
    const alias = tokens[k];
    if (alias && isName(alias)) aliases.set(keyOf(alias), ref);
  }
  return aliases;
}

/** The completion context at the end of `beforeCursor` (the statement text up to the cursor). */
export function completionContext(beforeCursor: string, statement: string): CompletionContext {
  const { tokens, endsInLiteral } = tokenize(beforeCursor);
  const empty = {
    qualifiers: [],
    partial: "",
    partialStart: beforeCursor.length,
    tableContext: false,
    aliases: new Map<string, TableRef>(),
  };
  if (endsInLiteral) return { suppressed: true, ...empty };

  // The partial word: a token touching the cursor (bare or an open/closed quoted identifier).
  let idx = tokens.length - 1;
  let partial = "";
  let partialStart = beforeCursor.length;
  const last = tokens[idx];
  const lastEnd = last ? (last.kind === "quoted" ? beforeCursor.length : last.start + last.text.length) : -1;
  if (
    last &&
    (last.kind === "ident" || last.kind === "quoted") &&
    lastEnd === beforeCursor.length &&
    !(last.kind === "quoted" && beforeCursor.endsWith('"') && beforeCursor.length - 1 > last.start)
  ) {
    partial = last.text;
    partialStart = last.start;
    idx--;
  }
  // Qualifiers: `name .` pairs right before the partial.
  const qualifiers: string[] = [];
  while (tokens[idx]?.kind === "dot" && (tokens[idx - 1]?.kind === "ident" || tokens[idx - 1]?.kind === "quoted")) {
    qualifiers.unshift(tokens[idx - 1]!.text);
    idx -= 2;
  }
  // Table context: the nearest keyword before the name is a table-introducing one (a comma in a
  // FROM list counts too).
  let tableContext = false;
  for (let k = idx; k >= 0; k--) {
    const t = tokens[k]!;
    if (t.kind === "comma") {
      // FROM a, b| — keep looking for FROM; SELECT a, b| — the SELECT ends it.
      continue;
    }
    if (t.kind !== "ident") {
      if (t.kind === "other" && (t.text === "(" || t.text === ")")) break;
      continue;
    }
    const upper = t.text.toUpperCase();
    if (TABLE_KEYWORDS.has(upper)) {
      tableContext =
        k === idx || tokens.slice(k + 1, idx + 1).every((x) => x.kind === "comma" || isName(x) || x.kind === "dot");
      break;
    }
    if (CLAUSE_KEYWORDS.has(upper)) break;
  }
  return { suppressed: false, qualifiers, partial, partialStart, tableContext, aliases: referencedTables(statement) };
}

const SIMPLE_IDENTIFIER = /^[a-z_][a-z0-9_]*$/;

/** How to write a name in SQL: bare when it's simple lower-case and not a keyword, else quoted. */
export function sqlIdentifier(name: string, keywords: Set<string>): string {
  return SIMPLE_IDENTIFIER.test(name) && !keywords.has(name.toUpperCase()) ? name : `"${name.replace(/"/g, '""')}"`;
}
