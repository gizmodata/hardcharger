// Upload data: a CSV, TSV, Excel, Parquet, JSON or Arrow file into a table, read and converted
// to Arrow in this browser (in a worker) and sent over Chargewire's bulk ingest. Its fields,
// labels and messages follow GizmoData Cloud's SQL editor (Upload data there), so the two read
// the same; the preview with editable column types is this UI's own, folded away until opened.

import { type Connection, ErrorCode, ChargewireError } from "@gizmodata/chargewire";
import { CheckCircle2, ChevronDown, ChevronRight, FileUp, Loader2, TriangleAlert, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { clearCatalogCaches, listingsFor } from "../lib/catalogs";
import { sqlIdentifier } from "../lib/completionContext";
import { delimiterLabel, typedDelimiter } from "../lib/upload/csv";
import {
  type UploadFormat,
  acceptedExtensions,
  defaultTableName,
  formatBytes,
  formatFromName,
  formatLabels,
  isDelimited,
  isExcel,
  nameProblem,
  wholeFileLimits,
} from "../lib/upload/formats";
import type { HeaderChoice } from "../lib/upload/infer";
import { UploadReader } from "../lib/upload/reader";
import type { UploadRequest } from "../lib/upload/request";
import {
  type UploadMode,
  type UploadProgress,
  type UploadResult,
  UploadRefused,
  runUpload,
  tableColumns,
  uploadConnection,
} from "../lib/upload/run";
import type { Inspection } from "../lib/upload/sources";
import {
  type Cell,
  type ColumnKind,
  type ColumnType,
  type OutputColumn,
  columnKinds,
  columnTypeOf,
  maxDecimalPrecision,
  sameType,
  typeSql,
} from "../lib/upload/types";
import { ValueError, cellText, converter, valueText } from "../lib/upload/values";
import { errorMessage, useSession } from "../session";

const SESSION_DEFAULT = "__session_default__";
const NEW_SCHEMA = "__new_schema__";

const kindLabels: Record<ColumnKind, string> = {
  VARCHAR: "VARCHAR",
  BIGINT: "BIGINT",
  DOUBLE: "DOUBLE",
  DECIMAL: "DECIMAL(p,s)",
  BOOLEAN: "BOOLEAN",
  DATE: "DATE",
  TIMESTAMP: "TIMESTAMP",
  TIMESTAMPTZ: "TIMESTAMPTZ",
  TIME: "TIME",
};

interface Choice {
  name: string;
  type: ColumnType;
  skip: boolean;
  /** The user picked the type (an append doesn't change it to the table's). */
  touched: boolean;
}

type TargetColumns =
  | { state: "none" }
  | { state: "loading" }
  | { state: "done"; columns: { name: string; type: string }[] }
  | { state: "error"; message: string };

/** A DECIMAL that holds the sample's numbers: their widest whole part and fraction. */
function suggestDecimal(cells: Iterable<Cell>): ColumnType {
  let whole = 1;
  let fraction = 0;
  let seen = false;
  for (const c of cells) {
    if (c === null) continue;
    const t = typeof c === "number" ? String(c) : typeof c === "string" ? c.trim() : "";
    const m = /^[+-]?(\d*)(?:\.(\d*))?$/.exec(t);
    if (!m) continue;
    seen = true;
    whole = Math.max(whole, (m[1] ?? "").replace(/^0+/, "").length || 1);
    fraction = Math.max(fraction, (m[2] ?? "").length);
  }
  if (!seen) return { kind: "DECIMAL", precision: 18, scale: 3 };
  const scale = Math.min(fraction, 18);
  const precision = Math.min(maxDecimalPrecision, Math.max(whole + scale + 2, 18));
  return { kind: "DECIMAL", precision, scale };
}

/** Brings an element into view when it appears (the progress bar, below a long form). */
const scrollIntoView = (el: HTMLElement | null) => el?.scrollIntoView({ block: "nearest" });

function messageOf(err: unknown): string {
  if (err instanceof UploadRefused) return err.message;
  if (ChargewireError.is(err)) {
    if (err.code === ErrorCode.permissionDenied) return `Permission denied: ${err.detail ?? err.message}`;
    if (err.code === ErrorCode.statementCancelled) return "Upload cancelled.";
  }
  return errorMessage(err);
}

interface Props {
  request: UploadRequest;
  /** Bumped with each new request (a file dropped on the page while the dialog is open). */
  serial: number;
  onClose: () => void;
}

export default function UploadDialog({ request, serial, onClose }: Props) {
  const session = useSession();
  const role = request.role ?? "";
  const listings = useMemo(() => listingsFor(session, role), [session, role]);
  const dialog = useRef<HTMLDialogElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const reader = useMemo(() => new UploadReader(), []);
  const [file, setFile] = useState<File | null>(null);
  // Bumped with each file picked, so a reading of an earlier one is never mistaken for it.
  const [fileId, setFileId] = useState(0);
  const [table, setTable] = useState("");
  const [tableTouched, setTableTouched] = useState(false);
  const [catalogs, setCatalogs] = useState<string[] | null>(null);
  const [catalog, setCatalog] = useState(request.catalog || SESSION_DEFAULT);
  const [schemaList, setSchemaList] = useState<{ catalog: string; names: string[] } | null>(null);
  const [schemaChoice, setSchemaChoice] = useState(request.schema || SESSION_DEFAULT);
  const [newSchema, setNewSchema] = useState("");
  const [mode, setMode] = useState<UploadMode>("create");
  const [header, setHeader] = useState<HeaderChoice>("auto");
  const [delimiter, setDelimiter] = useState("");
  const [delimiterSettled, setDelimiterSettled] = useState("");
  const [sheet, setSheet] = useState("");
  const [inspected, setInspected] = useState<{ key: string; inspection?: Inspection; error?: string } | null>(null);
  const [edits, setEdits] = useState<{ key: string; choices: Choice[] } | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [loadedTarget, setLoadedTarget] = useState<{ key: string; target: TargetColumns } | null>(null);
  const [progress, setProgress] = useState<UploadProgress | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<UploadResult | null>(null);
  const controller = useRef<AbortController | null>(null);
  const conn = useRef<Connection | null>(null);
  const [keywords, setKeywords] = useState<Set<string>>(new Set());
  // The session's own catalog and schema: where "Session default" puts the table.
  const [defaults, setDefaults] = useState({ catalog: "", schema: "main" });
  useEffect(() => {
    let live = true;
    session.connection.getSession().then(
      (doc) => {
        const o = doc.options ?? {};
        if (live) {
          setDefaults({
            catalog: typeof o["catalog"] === "string" ? o["catalog"] : "",
            schema: typeof o["schema"] === "string" && o["schema"] ? o["schema"] : "main",
          });
        }
      },
      () => {},
    );
    return () => {
      live = false;
    };
  }, [session]);

  useEffect(() => {
    dialog.current?.showModal();
    return () => {
      controller.current?.abort();
      reader.stop();
      void conn.current?.close().catch(() => {});
    };
  }, [reader]);

  useEffect(() => {
    let live = true;
    listings.catalogs().then(
      (names) => live && setCatalogs(names.filter((n) => n !== "temp" && n !== "system")),
      () => live && setCatalogs([]),
    );
    listings.vocabulary().then(
      (v) => live && setKeywords(new Set(v.keywords.map((k) => k.toUpperCase()))),
      () => {},
    );
    return () => {
      live = false;
    };
  }, [listings]);

  useEffect(() => {
    if (catalog === SESSION_DEFAULT) return;
    let live = true;
    listings.schemas(catalog).then(
      (names) => live && setSchemaList({ catalog, names }),
      () => live && setSchemaList({ catalog, names: [] }),
    );
    return () => {
      live = false;
    };
  }, [listings, catalog]);
  const schemas = catalog !== SESSION_DEFAULT && schemaList?.catalog === catalog ? schemaList.names : null;

  const excelTooBig = !!file && isExcel(formatFromName(file.name)) && file.size > wholeFileLimits.excel;

  const pickFile = (f: File | null) => {
    if (busy) return;
    setFile(f);
    setFileId((n) => n + 1);
    setError(null);
    setResult(null);
    setSheet("");
    setDelimiter("");
    setDelimiterSettled("");
    if (f && !tableTouched) setTable(defaultTableName(f.name));
  };

  // A file dropped on the page (also while the dialog is open).
  const [seenSerial, setSeenSerial] = useState(-1);
  if (seenSerial !== serial) {
    setSeenSerial(serial);
    if (request.file) pickFile(request.file);
  }

  // The delimiter settles a moment after typing stops.
  useEffect(() => {
    const t = window.setTimeout(() => setDelimiterSettled(delimiter), 350);
    return () => window.clearTimeout(t);
  }, [delimiter]);

  // Read the file's columns and first rows whenever it, or how to read it, changes.
  const readKey = JSON.stringify([fileId, header, delimiterSettled, sheet]);
  useEffect(() => {
    if (!file || excelTooBig) return;
    let live = true;
    reader.inspect(file, { header, delimiter: typedDelimiter(delimiterSettled) ?? "", sheet }).then(
      (found) => live && setInspected({ key: readKey, inspection: found }),
      (err: unknown) => live && setInspected({ key: readKey, error: err instanceof Error ? err.message : String(err) }),
    );
    return () => {
      live = false;
    };
  }, [file, excelTooBig, header, delimiterSettled, sheet, reader, readKey]);
  const current = file && !excelTooBig && inspected?.key === readKey ? inspected : undefined;
  const inspecting = !!file && !excelTooBig && !current;
  const inspection = current?.inspection ?? null;
  const readError = current?.error ?? null;
  const choices = useMemo<Choice[]>(
    () =>
      edits?.key === readKey
        ? edits.choices
        : (inspection?.columns.map((c) => ({ name: c.name, type: c.inferred, skip: false, touched: false })) ?? []),
    [edits, readKey, inspection],
  );
  const format: UploadFormat | undefined =
    inspection?.settings.format ?? (file ? formatFromName(file.name) : undefined);

  const schemaName =
    schemaChoice === NEW_SCHEMA ? newSchema.trim() : schemaChoice === SESSION_DEFAULT ? "" : schemaChoice;
  const catalogName = catalog === SESSION_DEFAULT ? "" : catalog;
  const targetText = [catalogName || null, schemaName || null, table.trim() || "…"].filter(Boolean).join(".");

  // An append: the table's columns, to match the file's to by name.
  const appendKey =
    mode === "append" && table.trim() && schemaChoice !== NEW_SCHEMA
      ? JSON.stringify([catalogName || defaults.catalog, schemaName || defaults.schema, table.trim()])
      : undefined;
  useEffect(() => {
    if (!appendKey) return;
    const [c, sch, t] = JSON.parse(appendKey) as [string, string, string];
    let live = true;
    const timer = window.setTimeout(() => {
      tableColumns(session.connection, c, sch, t).then(
        (columns) =>
          live &&
          setLoadedTarget({
            key: appendKey,
            target: { state: "done", columns: columns.map((col) => ({ name: col.name, type: col.type })) },
          }),
        (err: unknown) =>
          live && setLoadedTarget({ key: appendKey, target: { state: "error", message: errorMessage(err) } }),
      );
    }, 300);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [appendKey, session]);
  const target: TargetColumns = !appendKey
    ? { state: "none" }
    : loadedTarget?.key === appendKey
      ? loadedTarget.target
      : { state: "loading" };

  const tableCols = target.state === "done" && target.columns.length > 0 ? target.columns : undefined;

  /** The type a column loads as: the user's pick, else (appending) the table's, else the inferred one. */
  const effectiveType = useCallback(
    (i: number): ColumnType => {
      const c = choices[i]!;
      if (c.touched || !tableCols) return c.type;
      const t = tableCols.find((tc) => tc.name.toLowerCase() === c.name.toLowerCase());
      return (t && columnTypeOf(t.type)) ?? c.type;
    },
    [choices, tableCols],
  );

  // The columns the upload sends, and what's wrong with them.
  const plan = useMemo((): { columns: OutputColumn[]; problems: string[]; warnings: string[] } => {
    const problems: string[] = [];
    const warnings: string[] = [];
    const kept = choices.map((c, i) => ({ ...c, i })).filter((c) => !c.skip);
    if (inspection && kept.length === 0) problems.push("Every column is skipped: keep at least one.");
    const seen = new Map<string, string>();
    for (const c of kept) {
      const name = c.name.trim();
      if (!name) {
        problems.push(`Column ${c.i + 1} has no name.`);
        continue;
      }
      const key = name.toLowerCase();
      if (seen.has(key)) problems.push(`Two columns are named "${name}".`);
      seen.set(key, name);
    }
    if (mode === "append" && tableCols) {
      const shown = targetText;
      const extra = kept.filter((c) => !tableCols.some((t) => t.name.toLowerCase() === c.name.trim().toLowerCase()));
      if (extra.length > 0) {
        problems.push(
          `The file's columns don't fit ${shown} (append matches columns by name): the table has no column ` +
            `${extra.map((c) => `"${c.name.trim()}"`).join(", ")}. Rename or skip it under Preview & column types.`,
        );
      }
      const missing = tableCols.filter((t) => !kept.some((c) => c.name.trim().toLowerCase() === t.name.toLowerCase()));
      if (missing.length > 0 && extra.length === 0) {
        warnings.push(`The file has no ${missing.map((m) => `"${m.name}"`).join(", ")}: those columns get NULL.`);
      }
      const differ: string[] = [];
      for (const t of tableCols) {
        const c = kept.find((k) => k.name.trim().toLowerCase() === t.name.toLowerCase());
        if (!c) continue;
        const fixed = inspection?.columns[c.i]?.fixed;
        const mine = fixed ?? typeSql(effectiveType(c.i));
        const theirs = columnTypeOf(t.type);
        if (fixed ? fixed.toUpperCase() !== t.type.toUpperCase() : !theirs || !sameType(theirs, effectiveType(c.i))) {
          differ.push(`"${t.name}" is ${t.type} in the table, ${mine} in the file`);
        }
      }
      if (differ.length > 0) {
        warnings.push(
          `Column types differ: ${differ.join("; ")}. The server converts each value; one it can't convert fails the ` +
            "upload, and nothing is loaded.",
        );
      }
      const columns: OutputColumn[] = tableCols.map((t) => {
        const c = kept.find((k) => k.name.trim().toLowerCase() === t.name.toLowerCase());
        return c
          ? { name: t.name, type: effectiveType(c.i), source: c.i }
          : { name: t.name, type: { kind: "VARCHAR" }, source: null };
      });
      return { columns, problems, warnings };
    }
    return {
      columns: kept.map((c) => ({ name: c.name.trim(), type: effectiveType(c.i), source: c.i })),
      problems,
      warnings,
    };
  }, [choices, inspection, mode, tableCols, targetText, effectiveType]);

  const tableProblem = table.trim() ? nameProblem(table, "table") : undefined;
  const schemaProblem = schemaChoice === NEW_SCHEMA && newSchema.trim() ? nameProblem(newSchema, "schema") : undefined;
  const canSubmit =
    !!file &&
    !excelTooBig &&
    !!inspection &&
    !inspecting &&
    !!table.trim() &&
    !tableProblem &&
    !busy &&
    (schemaChoice !== NEW_SCHEMA || (!!newSchema.trim() && !schemaProblem)) &&
    plan.problems.length === 0 &&
    !(mode === "append" && target.state === "loading");

  const submit = async () => {
    if (!file || !inspection) return;
    const ac = new AbortController();
    controller.current = ac;
    setError(null);
    setResult(null);
    setBusy(true);
    setProgress({ phase: "uploading", fraction: 0, rows: 0, bytes: 0 });
    try {
      const c = await uploadConnection(session.openConnection, role);
      conn.current = c;
      const res = await runUpload({
        conn: c,
        target: {
          catalog: catalogName,
          schema: schemaName,
          table: table.trim(),
          createSchema: schemaChoice === NEW_SCHEMA,
          mode,
        },
        format: inspection.settings.format,
        chunks: reader.chunks(file, inspection.settings, plan.columns, inspection.context),
        signal: ac.signal,
        onProgress: setProgress,
      });
      setResult(res);
      // Show the new table in the schema browser and autocomplete.
      clearCatalogCaches(session);
    } catch (err) {
      if (ac.signal.aborted) setError("Upload cancelled.");
      else if (!session.checkSignIn(err)) setError(messageOf(err));
    } finally {
      reader.stop();
      void conn.current?.close().catch(() => {});
      conn.current = null;
      controller.current = null;
      setBusy(false);
      setProgress(null);
    }
  };

  const cancel = () => controller.current?.abort();
  const close = () => {
    if (!busy) onClose();
  };

  const queryTable = (res: UploadResult) => {
    const q = (n: string) => sqlIdentifier(n, keywords);
    session.requestQuery({
      sql: `SELECT * FROM ${q(res.catalog)}.${q(res.schema)}.${q(res.table)} LIMIT 100;`,
      run: true,
      newTab: true,
      title: res.table,
      catalog: res.catalog,
      schema: res.schema,
    });
    onClose();
  };

  const uploadAnother = () => {
    setResult(null);
    setFile(null);
    setTable("");
    setTableTouched(false);
    setFileId((n) => n + 1);
    if (fileInput.current) fileInput.current.value = "";
  };

  const setChoice = (i: number, patch: Partial<Choice>) =>
    setEdits({ key: readKey, choices: choices.map((c, j) => (j === i ? { ...c, ...patch } : c)) });

  const pickType = (i: number, value: string) => {
    if (value === "SKIP" || value === "UNSKIP") {
      setChoice(i, { skip: value === "SKIP" });
      return;
    }
    const kind = value as ColumnKind;
    const type =
      kind === "DECIMAL"
        ? suggestDecimal((inspection?.preview ?? []).map((r) => r[i] ?? null))
        : ({ kind } satisfies ColumnType);
    setChoice(i, { skip: false, type, touched: true });
  };

  const kind = format && isDelimited(format) ? "csv" : format && isExcel(format) ? "excel" : "other";
  const pct = progress ? Math.round(progress.fraction * 100) : 0;

  return (
    <dialog
      ref={dialog}
      className={`dialog upload-dialog${previewOpen && inspection && !result ? " upload-dialog-wide" : ""}`}
      onClose={onClose}
      onCancel={(e) => {
        e.preventDefault();
        close();
      }}
      aria-labelledby="upload-data-title"
      data-testid="upload-data-dialog"
    >
      <div className="dialog-head">
        <h2 id="upload-data-title" className="upload-title">
          <FileUp size={18} aria-hidden /> Upload data
        </h2>
        <button type="button" className="btn btn-ghost btn-icon" onClick={close} aria-label="Close" disabled={busy}>
          <X size={16} />
        </button>
      </div>
      <div className="dialog-body upload-body">
        <p className="upload-description">
          Load a CSV, TSV, Excel (.xlsx, .xls, .xlsb), Parquet, JSON / NDJSON or Arrow file into a table. Column types
          and the CSV dialect are detected; the load runs as you, and a failed upload leaves the table unchanged. Excel
          workbooks and JSON arrays are read whole in this browser (up to {formatBytes(wholeFileLimits.excel)}); the
          other formats are read as they upload, at any size.
        </p>

        {result ? (
          <div className="upload-result" data-testid="upload-data-result">
            <div className="upload-alert upload-alert-ok" role="status">
              <CheckCircle2 size={16} aria-hidden />
              <span>
                Loaded <strong>{result.rows.toLocaleString()}</strong> row{result.rows === 1 ? "" : "s"} into{" "}
                <code className="mono">
                  {result.catalog}.{result.schema}.{result.table}
                </code>{" "}
                ({result.mode}, {result.format.toUpperCase()}, {result.seconds.toFixed(1)}s).
              </span>
            </div>
            {result.columns.length > 0 && (
              <div className="upload-columns">
                <table>
                  <thead>
                    <tr>
                      <th>Column</th>
                      <th>Type</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.columns.map((c) => (
                      <tr key={c.name}>
                        <td className="mono">{c.name}</td>
                        <td className="mono upload-muted">{c.type}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        ) : (
          <>
            <label className="field">
              <span>File</span>
              <input
                ref={fileInput}
                id="upload-data-file"
                type="file"
                accept={acceptedExtensions}
                disabled={busy}
                aria-label="File"
                onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
              />
            </label>
            {file && (
              <p className={`upload-file-note${excelTooBig ? " upload-danger" : ""}`} data-testid="upload-file-note">
                {file.name} — {formatBytes(file.size)}
                {excelTooBig
                  ? ` (over the ${formatBytes(wholeFileLimits.excel)} limit for Excel workbooks, which this browser reads whole: save it as CSV and upload that)`
                  : ""}
                {inspecting && (
                  <span className="upload-muted">
                    {" "}
                    <Loader2 size={12} className="spin" aria-hidden /> Reading…
                  </span>
                )}
              </p>
            )}
            {readError && (
              <p className="field-error" role="alert" data-testid="upload-read-error">
                {readError}
              </p>
            )}

            <div className="upload-grid">
              <label className="field">
                <span>Catalog</span>
                <select
                  aria-label="Catalog"
                  value={catalog}
                  disabled={busy}
                  onChange={(e) => {
                    setCatalog(e.target.value);
                    setSchemaChoice(SESSION_DEFAULT);
                  }}
                >
                  <option value={SESSION_DEFAULT}>{catalogs === null ? "Loading…" : "Session default"}</option>
                  {catalog !== SESSION_DEFAULT && catalogs && !catalogs.includes(catalog) && (
                    <option value={catalog}>{catalog}</option>
                  )}
                  {(catalogs ?? []).map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              </label>
              <div className="upload-stack">
                <label className="field">
                  <span>Schema</span>
                  <select
                    aria-label="Schema"
                    value={schemaChoice}
                    disabled={busy}
                    onChange={(e) => setSchemaChoice(e.target.value)}
                  >
                    <option value={SESSION_DEFAULT}>Default (main)</option>
                    {schemaChoice !== SESSION_DEFAULT &&
                      schemaChoice !== NEW_SCHEMA &&
                      !(schemas ?? []).includes(schemaChoice) && <option value={schemaChoice}>{schemaChoice}</option>}
                    {(schemas ?? []).map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                    <option value={NEW_SCHEMA}>New schema…</option>
                  </select>
                </label>
                {schemaChoice === NEW_SCHEMA && (
                  <label className="field">
                    <input
                      aria-label="New schema name"
                      placeholder="new_schema"
                      value={newSchema}
                      disabled={busy}
                      onChange={(e) => setNewSchema(e.target.value)}
                    />
                  </label>
                )}
                {schemaProblem && <p className="field-error">{schemaProblem}</p>}
              </div>
            </div>

            <label className="field">
              <span>Table</span>
              <input
                id="upload-data-table"
                aria-label="Table"
                value={table}
                disabled={busy}
                placeholder="table_name"
                onChange={(e) => {
                  setTable(e.target.value);
                  setTableTouched(true);
                }}
              />
            </label>
            <p className="upload-target mono" data-testid="upload-target">
              → {targetText}
            </p>
            {tableProblem && <p className="field-error">{tableProblem}</p>}

            <fieldset className="upload-modes" disabled={busy}>
              <legend>If the table…</legend>
              {(
                [
                  ["create", "Create a new table (fails if it exists)"],
                  ["append", "Append to an existing table (columns matched by name)"],
                  ["replace", "Replace the table's contents"],
                ] as const
              ).map(([value, label]) => (
                <label key={value} className="upload-radio">
                  <input
                    type="radio"
                    name="upload-mode"
                    value={value}
                    checked={mode === value}
                    onChange={() => setMode(value)}
                  />
                  {label}
                </label>
              ))}
            </fieldset>

            {(kind === "csv" || kind === "excel") && (
              <div className="upload-grid">
                <label className="field">
                  <span>Header row</span>
                  <select
                    aria-label="Header row"
                    value={header}
                    disabled={busy}
                    onChange={(e) => setHeader(e.target.value as HeaderChoice)}
                  >
                    <option value="auto">Detect</option>
                    <option value="yes">First row is column names</option>
                    <option value="no">No header row</option>
                  </select>
                </label>
                {kind === "csv" ? (
                  <label className="field">
                    <span>Delimiter</span>
                    <input
                      id="upload-data-delimiter"
                      aria-label="Delimiter"
                      value={delimiter}
                      maxLength={4}
                      disabled={busy}
                      placeholder={
                        inspection?.settings.delimiter
                          ? `Detect (${delimiterLabel(inspection.settings.delimiter)})`
                          : "Detect"
                      }
                      onChange={(e) => setDelimiter(e.target.value)}
                    />
                  </label>
                ) : (
                  <label className="field">
                    <span>Worksheet</span>
                    <select
                      aria-label="Worksheet"
                      value={sheet}
                      disabled={busy || !inspection?.sheets}
                      onChange={(e) => setSheet(e.target.value)}
                    >
                      <option value="">First sheet</option>
                      {(inspection?.sheets ?? []).map((s, i) => (
                        <option key={s} value={s}>
                          {i === 0 ? `${s} (first sheet)` : s}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
              </div>
            )}

            {inspection && (
              <section className="upload-preview-section">
                <button
                  type="button"
                  className="upload-preview-toggle"
                  aria-expanded={previewOpen}
                  aria-controls="upload-preview"
                  onClick={() => setPreviewOpen((o) => !o)}
                  data-testid="upload-preview-toggle"
                >
                  {previewOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                  Preview &amp; column types
                  <span className="upload-muted">
                    {" "}
                    · {choices.filter((c) => !c.skip).length.toLocaleString()} of {choices.length.toLocaleString()}{" "}
                    columns
                    {inspection.rows !== undefined ? ` · ${inspection.rows.toLocaleString()} rows` : ""}
                    {format ? ` · ${formatLabels[format]}` : ""}
                    {inspection.settings.delimiter
                      ? `, delimiter ${JSON.stringify(delimiterLabel(inspection.settings.delimiter))}`
                      : ""}
                    {kind !== "other" ? (inspection.settings.header ? ", header row" : ", no header row") : ""}
                    {inspection.settings.sheet ? `, sheet ${inspection.settings.sheet}` : ""}
                  </span>
                </button>
                {inspection.notes.length > 0 && (
                  <ul className="upload-notes" data-testid="upload-notes">
                    {inspection.notes.map((n) => (
                      <li key={n}>{n}</li>
                    ))}
                  </ul>
                )}
                {previewOpen && (
                  <Preview
                    id="upload-preview"
                    inspection={inspection}
                    choices={choices}
                    effectiveType={effectiveType}
                    disabled={busy}
                    onRename={(i, name) => setChoice(i, { name })}
                    onType={pickType}
                    onDecimal={(i, precision, scale) =>
                      setChoice(i, { type: { kind: "DECIMAL", precision, scale }, touched: true })
                    }
                  />
                )}
              </section>
            )}

            {mode === "append" && target.state === "error" && (
              <div className="upload-alert upload-alert-warn" role="status">
                <TriangleAlert size={16} aria-hidden />{" "}
                <span>The table's columns couldn't be read: {target.message}</span>
              </div>
            )}
            {plan.warnings.map((w) => (
              <div key={w} className="upload-alert upload-alert-warn" role="status" data-testid="upload-warning">
                <TriangleAlert size={16} aria-hidden /> <span>{w}</span>
              </div>
            ))}
            {plan.problems.map((p) => (
              <div key={p} className="upload-alert upload-alert-error" role="alert" data-testid="upload-problem">
                <TriangleAlert size={16} aria-hidden /> <span>{p}</span>
              </div>
            ))}

            {progress && busy && (
              <div className="upload-progress" data-testid="upload-data-progress" ref={scrollIntoView}>
                <div
                  className="upload-bar"
                  role="progressbar"
                  aria-label="Upload progress"
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={pct}
                >
                  <div className="upload-bar-fill" style={{ width: `${pct}%` }} />
                </div>
                <p className="upload-muted">
                  <Loader2 size={13} className="spin" aria-hidden />{" "}
                  {progress.phase === "uploading"
                    ? `Uploading… ${pct}% · ${progress.rows.toLocaleString()} rows · ${formatBytes(progress.bytes)} sent`
                    : `Committing ${progress.rows.toLocaleString()} rows…`}
                </p>
              </div>
            )}

            {error && (
              <div className="upload-alert upload-alert-error" role="alert" data-testid="upload-error">
                <TriangleAlert size={16} aria-hidden /> <span>{error}</span>
              </div>
            )}
          </>
        )}
      </div>
      <div className="dialog-foot upload-foot">
        {result ? (
          <>
            <button type="button" className="btn btn-secondary" onClick={uploadAnother}>
              Upload another
            </button>
            <button type="button" className="btn btn-secondary" onClick={() => queryTable(result)}>
              Query this table
            </button>
            <button type="button" className="btn btn-primary" onClick={onClose}>
              Done
            </button>
          </>
        ) : (
          <>
            {busy ? (
              <button
                type="button"
                className="btn btn-secondary"
                onClick={cancel}
                disabled={progress?.phase === "committing"}
              >
                Cancel upload
              </button>
            ) : (
              <button type="button" className="btn btn-secondary" onClick={close}>
                Close
              </button>
            )}
            <button type="button" className="btn btn-primary" onClick={() => void submit()} disabled={!canSubmit}>
              {busy ? <Loader2 size={14} className="spin" aria-hidden /> : <FileUp size={14} aria-hidden />} Upload
            </button>
          </>
        )}
      </div>
    </dialog>
  );
}

function Preview(props: {
  id: string;
  inspection: Inspection;
  choices: Choice[];
  effectiveType: (i: number) => ColumnType;
  disabled: boolean;
  onRename: (i: number, name: string) => void;
  onType: (i: number, value: string) => void;
  onDecimal: (i: number, precision: number, scale: number) => void;
}) {
  const { inspection, choices, effectiveType } = props;
  const converters = useMemo(
    () =>
      choices.map((_, i) => {
        const fixed = inspection.columns[i]?.fixed;
        const type = effectiveType(i);
        const conv = converter(type, inspection.context);
        return (cell: Cell): { text: string; bad?: string } => {
          if (cell === null || (type.kind !== "VARCHAR" && typeof cell === "string" && cell.trim() === "")) {
            return { text: "NULL" };
          }
          if (fixed) return { text: cellText(cell) };
          try {
            const v = conv(cell);
            return v === null ? { text: "NULL" } : { text: valueText(type, v) };
          } catch (err) {
            if (!(err instanceof ValueError)) throw err;
            return { text: cellText(cell), bad: `${err.message}: this value fails the upload as ${typeSql(type)}` };
          }
        };
      }),
    [choices, inspection, effectiveType],
  );
  return (
    <div className="upload-preview" id={props.id} data-testid="upload-preview">
      <p className="upload-muted">
        The first {inspection.preview.length.toLocaleString()} rows as they will load. Rename a column, change its type,
        or skip it; a value shown in red doesn't fit its type and would fail the upload.
        {inspection.columns.some((c) => c.fixed) ? " Parquet and Arrow columns keep the file's own types." : ""}
      </p>
      <div className="upload-preview-scroll">
        <table>
          <thead>
            <tr>
              {choices.map((c, i) => {
                const fixed = inspection.columns[i]?.fixed;
                const type = effectiveType(i);
                return (
                  <th key={i} className={c.skip ? "upload-skipped" : undefined}>
                    <input
                      className="upload-colname"
                      aria-label={`Name of column ${i + 1}`}
                      value={c.name}
                      disabled={props.disabled || c.skip}
                      onChange={(e) => props.onRename(i, e.target.value)}
                    />
                    <select
                      className="upload-coltype"
                      aria-label={`Type of column ${i + 1}`}
                      value={c.skip ? "SKIP" : fixed ? "FIXED" : type.kind}
                      disabled={props.disabled}
                      onChange={(e) => props.onType(i, e.target.value === "FIXED" ? "UNSKIP" : e.target.value)}
                    >
                      {fixed ? (
                        <option value="FIXED">{fixed}</option>
                      ) : (
                        columnKinds.map((k) => (
                          <option key={k} value={k}>
                            {kindLabels[k]}
                          </option>
                        ))
                      )}
                      <option value="SKIP">Skip column</option>
                    </select>
                    {!fixed && !c.skip && type.kind === "DECIMAL" && (
                      <span className="upload-decimal">
                        <input
                          type="number"
                          min={1}
                          max={maxDecimalPrecision}
                          aria-label={`Precision of column ${i + 1}`}
                          value={type.precision ?? 18}
                          disabled={props.disabled}
                          onChange={(e) => {
                            const p = Math.min(maxDecimalPrecision, Math.max(1, Number(e.target.value) || 1));
                            props.onDecimal(i, p, Math.min(type.scale ?? 3, p));
                          }}
                        />
                        <input
                          type="number"
                          min={0}
                          max={type.precision ?? 18}
                          aria-label={`Scale of column ${i + 1}`}
                          value={type.scale ?? 3}
                          disabled={props.disabled}
                          onChange={(e) => {
                            const p = type.precision ?? 18;
                            props.onDecimal(i, p, Math.min(p, Math.max(0, Number(e.target.value) || 0)));
                          }}
                        />
                      </span>
                    )}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {inspection.preview.map((row, r) => (
              <tr key={r}>
                {choices.map((c, i) => {
                  const shown = converters[i]!(row[i] ?? null);
                  const cls = [
                    c.skip ? "upload-skipped" : "",
                    shown.text === "NULL" && (row[i] ?? null) === null ? "upload-null" : "",
                    shown.bad ? "upload-bad" : "",
                  ]
                    .filter(Boolean)
                    .join(" ");
                  return (
                    <td key={i} className={cls || undefined} title={shown.bad}>
                      {shown.text}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
