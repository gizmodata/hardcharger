import { useEffect, useMemo, useRef, useState } from "react";

import { benchmarkQuery, detectBenchmarks, type Benchmark } from "../lib/benchmarks";
import type { CatalogCache } from "../lib/catalogCache";

interface Props {
  listings: Pick<CatalogCache, "tables">;
  catalog: string;
  schema: string;
  version: number;
  active: boolean;
  disabled: boolean;
  onOpen: (query: { title: string; sql: string; catalog: string; schema: string }) => void;
}

/** Uses the same role/server-scoped metadata as the schema browser, never scans user data. */
export function BenchmarkQueries({ listings, catalog, schema, version, active, disabled, onOpen }: Props) {
  const key = JSON.stringify([catalog, schema, version]);
  const [found, setFound] = useState<{ key: string; listings: Props["listings"]; suites: Benchmark[] }>();
  const scope = useMemo(() => ({ listings, key, active }), [listings, key, active]);
  const request = useRef<AbortController | undefined>(undefined);
  const [pending, setPending] = useState<typeof scope>();
  const [failure, setFailure] = useState<{ scope: typeof scope; message: string }>();
  const loading = pending === scope;
  const error = failure?.scope === scope ? failure.message : undefined;
  useEffect(() => {
    let live = true;
    const controller = new AbortController();
    request.current = controller;
    if (active && catalog && schema) {
      void listings.tables(catalog, schema).then(
        (tables) => {
          if (live) setFound({ key, listings, suites: detectBenchmarks(tables) });
        },
        () => {
          if (live) setFound(undefined);
        },
      );
    }
    return () => {
      live = false;
      controller.abort();
    };
  }, [listings, catalog, schema, key, active, scope]);

  const suites = active && found?.key === key && found.listings === listings ? found.suites : [];
  if (!suites.length) return null;
  const choose = async (value: string) => {
    const [id, number] = value.split(":");
    const suite = suites.find((b) => b.id === id);
    if (!suite || disabled || loading) return;
    const controller = request.current;
    if (!controller) return;
    setPending(scope);
    setFailure(undefined);
    try {
      const sql = await benchmarkQuery(suite.id, Number(number));
      if (!controller.signal.aborted) onOpen({ title: `${suite.name} Q${number}`, sql, catalog, schema });
    } catch {
      if (!controller.signal.aborted)
        setFailure({ scope, message: "Could not load the benchmark query. Please try again." });
    } finally {
      if (!controller.signal.aborted) setPending(undefined);
    }
  };
  return (
    <>
      <label
        className="toolbar-field"
        title={disabled ? "Close a tab to open a benchmark query" : "Open an example in a new tab; review it, then Run"}
      >
        Benchmark
        <select
          aria-label="Benchmark query"
          value=""
          disabled={disabled || loading}
          onChange={(event) => void choose(event.target.value)}
        >
          <option value="">{loading ? "Loading…" : "Choose query…"}</option>
          {suites.map((suite) => (
            <optgroup key={suite.id} label={suite.name}>
              {Array.from({ length: suite.count }, (_, i) => (
                <option key={i} value={`${suite.id}:${i + 1}`}>
                  {suite.name} Q{i + 1}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      </label>
      {error && (
        <span role="alert" className="error-text">
          {error}
        </span>
      )}
    </>
  );
}
