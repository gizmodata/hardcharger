// SQL text helpers: splitting a script into statements (for "run the statement under the
// cursor") and quoting identifiers. The scanner knows DuckDB's lexical rules for the parts
// that can hide a semicolon: 'strings' (with '' inside), "identifiers" (with "" inside),
// E'escaped\' strings', $tag$dollar-quoted$tag$ strings, -- line comments and nested
// /* block comments */.

export interface Statement {
  /** Offset of the statement's first character that is not whitespace. */
  start: number;
  /** Offset just past its last character, before the `;` that ends it. */
  end: number;
  text: string;
}

interface Region {
  from: number; // first offset of the region (just after the previous `;`)
  to: number; // offset of the `;` that ends it, or the text's length
  statement: Statement | undefined; // undefined: only whitespace and comments
}

function regions(sql: string): Region[] {
  const out: Region[] = [];
  let from = 0;
  let firstCode = -1; // first offset of something other than whitespace or a comment
  let lastCode = -1;
  let i = 0;
  const n = sql.length;
  const close = (to: number) => {
    out.push({
      from,
      to,
      statement:
        firstCode < 0 ? undefined : { start: firstCode, end: lastCode + 1, text: sql.slice(firstCode, lastCode + 1) },
    });
    from = to + 1;
    firstCode = -1;
    lastCode = -1;
  };
  const code = (a: number, b: number) => {
    if (firstCode < 0) firstCode = a;
    lastCode = b;
  };
  while (i < n) {
    const c = sql[i]!;
    const next = sql[i + 1];
    if (c === ";") {
      close(i);
      i++;
      continue;
    }
    if (c === "-" && next === "-") {
      const nl = sql.indexOf("\n", i + 2);
      i = nl < 0 ? n : nl + 1;
      continue;
    }
    if (c === "/" && next === "*") {
      let depth = 1;
      let j = i + 2;
      while (j < n && depth > 0) {
        if (sql[j] === "/" && sql[j + 1] === "*") {
          depth++;
          j += 2;
        } else if (sql[j] === "*" && sql[j + 1] === "/") {
          depth--;
          j += 2;
        } else j++;
      }
      i = j;
      continue;
    }
    if (c === "'" || c === '"') {
      const escaped = c === "'" && i > 0 && /[eE]/.test(sql[i - 1]!) && !/[\w$]/.test(sql[i - 2] ?? "");
      let j = i + 1;
      while (j < n) {
        if (escaped && sql[j] === "\\") {
          j += 2;
          continue;
        }
        if (sql[j] === c) {
          if (sql[j + 1] === c) {
            j += 2;
            continue;
          }
          break;
        }
        j++;
      }
      code(i, Math.min(j, n - 1));
      i = j + 1;
      continue;
    }
    if (c === "$") {
      const tag = /^\$([A-Za-z_][A-Za-z0-9_]*)?\$/.exec(sql.slice(i));
      // $1 is a parameter, and a tag can't follow an identifier character (a$b$ is a name).
      if (tag && !/[\w$]/.test(sql[i - 1] ?? "")) {
        const endAt = sql.indexOf(tag[0], i + tag[0].length);
        const stop = endAt < 0 ? n : endAt + tag[0].length;
        code(i, stop - 1);
        i = stop;
        continue;
      }
    }
    if (!/\s/.test(c)) code(i, i);
    i++;
  }
  close(n);
  return out;
}

/** The statements of a script, in order, without empty ones. */
export function splitStatements(sql: string): Statement[] {
  return regions(sql)
    .map((r) => r.statement)
    .filter((s): s is Statement => s !== undefined);
}

/**
 * The statement the cursor is in. A cursor just after a `;`, or on blank lines between
 * statements, belongs to the statement before it; at the very top, to the first one.
 */
export function statementAt(sql: string, offset: number): Statement | undefined {
  const all = regions(sql);
  let index = all.findIndex((r) => offset >= r.from && offset <= r.to);
  if (index < 0) index = all.length - 1;
  const here = all[index]!;
  if (here.statement && offset >= here.statement.start - lineStartGap(sql, here.statement.start)) {
    return here.statement;
  }
  for (let i = index - 1; i >= 0; i--) {
    const s = all[i]!.statement;
    if (s) return s;
  }
  return here.statement ?? all.slice(index + 1).find((r) => r.statement)?.statement;
}

// Characters between the start of the statement's first line and the statement: a cursor
// at the start of that line is in the statement, not in the one before.
function lineStartGap(sql: string, start: number): number {
  let i = start;
  while (i > 0 && sql[i - 1] !== "\n" && /\s/.test(sql[i - 1]!)) i--;
  return start - i;
}

/** A SQL identifier, double-quoted when it needs to be (or always, to be safe). */
export function quoteIdentifier(name: string): string {
  return `"${name.replaceAll('"', '""')}"`;
}

/** A SQL string literal. */
export function quoteLiteral(value: string): string {
  return `'${value.replaceAll("'", "''")}'`;
}

/** A LIKE pattern that matches exactly `name` (Chargewire metadata filters are LIKE patterns). */
export function exactLike(name: string): string {
  return name.replace(/[\\%_]/g, (c) => `\\${c}`);
}

/** A statement's first word, upper-cased, after any leading comments ("" when there is none). */
export function firstKeyword(text: string): string {
  let rest = text.trimStart();
  for (;;) {
    if (rest.startsWith("--")) {
      const nl = rest.indexOf("\n");
      rest = nl < 0 ? "" : rest.slice(nl + 1).trimStart();
    } else if (rest.startsWith("/*")) {
      let depth = 1;
      let i = 2;
      while (i < rest.length && depth > 0) {
        if (rest.startsWith("/*", i)) {
          depth++;
          i += 2;
        } else if (rest.startsWith("*/", i)) {
          depth--;
          i += 2;
        } else i++;
      }
      rest = rest.slice(i).trimStart();
    } else break;
  }
  return /^[A-Za-z_]+/.exec(rest)?.[0].toUpperCase() ?? "";
}

const catalogChanging = new Set(["CREATE", "DROP", "ALTER", "ATTACH", "DETACH", "IMPORT", "COMMENT", "USE"]);
const transactionWords = new Set(["BEGIN", "START", "COMMIT", "END", "ROLLBACK", "ABORT"]);

/** Whether running `sql` may add, drop or rename a catalog, schema, table, view or column. */
export function mayChangeCatalog(sql: string): boolean {
  return splitStatements(sql).some((s) => catalogChanging.has(firstKeyword(s.text)));
}

/** Whether a statement begins or ends a transaction (BEGIN, COMMIT, ...). */
export function isTransactionStatement(sql: string): boolean {
  return transactionWords.has(firstKeyword(sql));
}

/** A statement's first line, squeezed and shortened, for a label. */
export function statementPreview(sql: string, max = 40): string {
  const line = sql.replace(/\s+/g, " ").trim();
  return line.length > max ? `${line.slice(0, max)}…` : line;
}
