import { Copy, WandSparkles } from "lucide-react";
import { useMemo, useState } from "react";
import { copyText } from "../lib/clipboard";
import { formatSql, sqlFormatStyle } from "../lib/sqlFormat";

/** Read-only SQL: the toggle and Copy use the same view; the recorded text is never changed. */
export function SqlText({ sql, internal = false }: { sql: string; internal?: boolean }) {
  const [formatted, setFormatted] = useState(false);
  const [notice, setNotice] = useState("");
  const view = useMemo(() => (formatted ? formatSql(sql) : { text: sql }), [sql, formatted]);
  return (
    <div className="monitor-sql-block">
      <div className="monitor-sql-head">
        <span role="status">{notice || (formatted && !view.error ? "Formatted SQL" : "Original SQL")}</span>
        <div className="sql-text-actions">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            aria-pressed={formatted}
            title={sqlFormatStyle}
            onClick={() => {
              setFormatted(!formatted);
              setNotice("");
            }}
          >
            <WandSparkles size={14} /> {formatted ? "Original" : "Formatted"}
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() =>
              void copyText(view.text).then((ok) =>
                setNotice(ok ? "Copied to clipboard." : "Copy failed; select the SQL below."),
              )
            }
          >
            <Copy size={14} /> {internal ? "Copy internal SQL" : "Copy SQL"}
          </button>
        </div>
      </div>
      {view.error && (
        <p className="page-sub" role="status">
          {view.error}
        </p>
      )}
      <pre className="value-text" tabIndex={0} aria-label={internal ? "Internal SQL text" : "SQL text"}>
        {view.text}
      </pre>
    </div>
  );
}
