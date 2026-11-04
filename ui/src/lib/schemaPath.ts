// How a schema's name arrives from Chargewire metadata (db_schema_name), and how the UI
// spells it back in SQL. DuckDB 2.0 nests schemas (CREATE SCHEMA sales; CREATE SCHEMA
// sales.payroll), and the server lists a schema as its path from the top, the parts joined
// by dots; a part is written "…" (inner quotes doubled) only when it holds a dot or a double
// quote, so a top-level schema whose own name holds a dot arrives as `"sales.payroll"` and the
// nested `sales.payroll` can be told apart. Everything else is written bare even when SQL
// would need quotes (`My Schema` arrives as `My Schema`), so the metadata string is never
// pasted into SQL: each part goes through the identifier rule. The filters the listings take
// (a schema pattern, a db_schema parameter) match that same text, so the metadata spelling
// goes back to them unchanged. This is the one place that reads or writes the spelling: the
// schema browser's tree, its drag names, the search and the listings' filters all go
// through here, so a change to the contract is one edit.

/** The parts of a schema's spelling, outermost first: `a.b` → ["a", "b"], `"a.b"` → ["a.b"]. */
export function parseSchemaName(spelled: string): string[] {
  const parts: string[] = [];
  let part = "";
  let quoted = false;
  let i = 0;
  while (i < spelled.length) {
    const c = spelled[i]!;
    if (quoted) {
      if (c === '"') {
        if (spelled[i + 1] === '"') {
          part += '"';
          i += 2;
          continue;
        }
        quoted = false;
        i++;
        continue;
      }
      part += c;
      i++;
      continue;
    }
    if (c === '"') {
      quoted = true;
      i++;
      continue;
    }
    if (c === ".") {
      parts.push(part);
      part = "";
      i++;
      continue;
    }
    part += c;
    i++;
  }
  parts.push(part);
  return parts;
}

/** A schema path as SQL, each part quoted by `ident` when it needs it. */
export function schemaSql(path: readonly string[], ident: (name: string) => string): string {
  return path.map(ident).join(".");
}

/** A schema path as the server spells it in metadata: dots between parts, "…" only around a part with a dot or quote. */
export function spellSchemaPath(path: readonly string[]): string {
  return path.map((p) => (p.includes(".") || p.includes('"') ? `"${p.replaceAll('"', '""')}"` : p)).join(".");
}

/** The SQL that spells a schema name column the same way (for queries over duckdb_schemas()). */
export const spellSchemaPartSql = (column: string): string =>
  `CASE WHEN ${column} LIKE '%.%' OR ${column} LIKE '%"%' THEN '"' || replace(${column}, '"', '""') || '"' ELSE ${column} END`;

export interface SchemaNode {
  /** The schema's own name (the last part of its path). */
  name: string;
  /** Its spelling as the server lists it: what the listings take as the schema. */
  spelled: string;
  path: string[];
  /** False for a parent the listing lacks (the user may see a child schema but not it). */
  listed: boolean;
  children: SchemaNode[];
}

/** The schemas of a catalog as a tree, in the listing's order, nested ones under their parents. */
export function schemaTree(spellings: readonly string[]): SchemaNode[] {
  const roots: SchemaNode[] = [];
  const byKey = new Map<string, SchemaNode>();
  const key = (path: readonly string[]) => JSON.stringify(path);
  const place = (path: string[], spelled: string | undefined): SchemaNode => {
    const k = key(path);
    const found = byKey.get(k);
    if (found) {
      if (spelled !== undefined && !found.listed) {
        found.listed = true;
        found.spelled = spelled;
      }
      return found;
    }
    const node: SchemaNode = {
      name: path[path.length - 1] ?? "",
      spelled: spelled ?? spellSchemaPath(path),
      path,
      listed: spelled !== undefined,
      children: [],
    };
    byKey.set(k, node);
    if (path.length <= 1) roots.push(node);
    else place(path.slice(0, -1), undefined).children.push(node);
    return node;
  };
  for (const spelled of spellings) place(parseSchemaName(spelled), spelled);
  return roots;
}
