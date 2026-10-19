// Autocomplete in the SQL editor: catalogs, schemas, tables and views, columns (with their
// types), the engine's functions (inserted with parentheses) and keywords, from the server's
// metadata as the user may see it (CatalogCache). Monaco's completion providers are global per
// language, so one provider serves every editor; each editor's model registers a getter for
// its tab's cache, catalog and schema.

import type { CatalogCache } from "./catalogCache";
import { type TableRef, completionContext, sqlIdentifier } from "./completionContext";
import { monaco } from "./monaco";
import { statementAt } from "./sql";

export interface CompletionTarget {
  cache: CatalogCache;
  /** The tab's session catalog and schema. */
  catalog: string;
  schema: string;
}

const targets = new Map<string, () => CompletionTarget | undefined>();

/** Makes a model's completions follow its tab; returns the way to stop. */
export function registerCompletionTarget(
  model: monaco.editor.ITextModel,
  get: () => CompletionTarget | undefined,
): () => void {
  const key = model.uri.toString();
  targets.set(key, get);
  return () => {
    if (targets.get(key) === get) targets.delete(key);
  };
}

const safe = async <T>(p: Promise<T>, fallback: T): Promise<T> => {
  try {
    return await p;
  } catch {
    return fallback;
  }
};

/** A typed name matched to a listed one: exact first ("quoted" keeps case), then any case. */
const find = (names: readonly string[], typed: string) =>
  names.find((n) => n === typed) ?? names.find((n) => n.toLowerCase() === typed.toLowerCase());

async function resolveTable(t: CompletionTarget, ref: TableRef): Promise<[string, string, string] | undefined> {
  const catalogs = await safe(t.cache.catalogs(), []);
  const catalog = ref.catalog ? find(catalogs, ref.catalog) : t.catalog || undefined;
  if (!catalog) return undefined;
  const schema = find(await safe(t.cache.schemas(catalog), []), ref.schema ?? (t.schema || "main"));
  if (!schema) return undefined;
  const table = find(
    (await safe(t.cache.tables(catalog, schema), [])).map((x) => x.name),
    ref.table,
  );
  return table ? [catalog, schema, table] : undefined;
}

// Sort order: columns, tables, schemas, catalogs, functions, keywords.
const order = { column: "0", table: "1", schema: "2", catalog: "3", func: "4", keyword: "5" } as const;

let registered = false;

/** Registers the one SQL completion provider (once). */
export function registerSqlCompletion(): void {
  if (registered) return;
  registered = true;
  const K = monaco.languages.CompletionItemKind;
  monaco.languages.registerCompletionItemProvider("sql", {
    triggerCharacters: [".", '"'],
    async provideCompletionItems(model, position) {
      const target = targets.get(model.uri.toString())?.();
      if (!target) return { suggestions: [] };
      const text = model.getValue();
      const offset = model.getOffsetAt(position);
      const statement = statementAt(text, offset);
      const start = statement && statement.start <= offset ? statement.start : 0;
      const before = text.slice(start, offset);
      const ctx = completionContext(before, statement?.text ?? before);
      if (ctx.suppressed) return { suggestions: [] };

      const from = model.getPositionAt(start + ctx.partialStart);
      const range = new monaco.Range(from.lineNumber, from.column, position.lineNumber, position.column);
      const vocabulary = await safe(target.cache.vocabulary(), { keywords: [], functions: [] });
      const keywords = new Set(vocabulary.keywords.map((k) => k.toUpperCase()));
      const ident = (name: string) => sqlIdentifier(name, keywords);
      const suggestions: monaco.languages.CompletionItem[] = [];
      const add = (
        label: string,
        kind: monaco.languages.CompletionItemKind,
        sort: string,
        detail?: string,
        insertText = ident(label),
      ) =>
        suggestions.push({
          label,
          kind,
          ...(detail ? { detail } : {}),
          insertText,
          range,
          sortText: `${sort}${label.toLowerCase()}`,
          filterText: label,
        });

      const addColumns = async (ref: TableRef) => {
        const found = await resolveTable(target, ref);
        if (!found) return;
        for (const c of await safe(target.cache.columns(...found), [])) add(c.name, K.Field, order.column, c.type);
      };
      const addTables = async (catalog: string, schema: string) => {
        for (const t of await safe(target.cache.tables(catalog, schema), [])) {
          add(t.name, t.isView ? K.Interface : K.Struct, order.table, t.isView ? "view" : "table");
        }
      };
      const addSchemas = async (catalog: string) => {
        for (const s of await safe(target.cache.schemas(catalog), []))
          add(s, K.Module, order.schema, `schema in ${catalog}`);
      };
      const catalogs = await safe(target.cache.catalogs(), []);
      const q = ctx.qualifiers;

      if (q.length === 1) {
        const name = q[0]!;
        const alias = ctx.aliases.get(name) ?? ctx.aliases.get(name.toLowerCase());
        const catalog = find(catalogs, name);
        if (alias) await addColumns(alias);
        else if (catalog) await addSchemas(catalog);
        else if (target.catalog) {
          const schema = find(await safe(target.cache.schemas(target.catalog), []), name);
          if (schema) await addTables(target.catalog, schema);
          else await addColumns({ table: name });
        }
      } else if (q.length === 2) {
        const catalog = find(catalogs, q[0]!);
        const schema = catalog ? find(await safe(target.cache.schemas(catalog), []), q[1]!) : undefined;
        if (catalog && schema) await addTables(catalog, schema);
        else await addColumns({ schema: q[0]!, table: q[1]! });
      } else if (q.length >= 3) {
        await addColumns({ catalog: q[0]!, schema: q[1]!, table: q[2]! });
      } else if (ctx.tableContext) {
        if (target.catalog) {
          await addTables(target.catalog, target.schema || "main");
          await addSchemas(target.catalog);
        }
        for (const c of catalogs) add(c, K.Module, order.catalog, "catalog");
      } else {
        // Anywhere else: the referenced tables' aliases and columns, the functions, the
        // keywords, and the current schema's tables.
        const seen = new Set<string>();
        for (const [name, ref] of ctx.aliases) {
          const key = `${ref.catalog ?? ""}.${ref.schema ?? ""}.${ref.table}`.toLowerCase();
          if (name !== ref.table.toLowerCase()) add(name, K.Variable, order.table, `alias for ${ref.table}`, name);
          if (seen.has(key)) continue;
          seen.add(key);
          await addColumns(ref);
        }
        for (const f of vocabulary.functions) {
          const call = ["scalar", "aggregate", "table", "macro", "table_macro"].includes(f.type);
          suggestions.push({
            label: f.name,
            kind: K.Function,
            detail: `${f.type.replace("_", " ")} function`,
            range,
            insertText: call ? `${f.name}($0)` : f.name,
            insertTextRules: monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet,
            sortText: `${order.func}${f.name}`,
          });
        }
        for (const k of vocabulary.keywords) {
          suggestions.push({
            label: k.toUpperCase(),
            kind: K.Keyword,
            insertText: k.toUpperCase(),
            range,
            sortText: `${order.keyword}${k.toLowerCase()}`,
          });
        }
        if (target.catalog) await addTables(target.catalog, target.schema || "main");
      }
      return { suggestions };
    },
  });
}
