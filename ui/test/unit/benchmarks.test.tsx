import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BenchmarkQueries } from "../../src/components/BenchmarkQueries";
import { benchmarkQuery, benchmarks, detectBenchmarks } from "../../src/lib/benchmarks";
import type { TableEntry } from "../../src/lib/catalogCache";

const tables = (names: readonly string[]): TableEntry[] =>
  names.map((name) => ({ name, type: "TABLE", isView: false }));
const tpch = tables(benchmarks[0].tables);
const tpcds = tables(benchmarks[1].tables);
afterEach(cleanup);

describe("benchmark examples", () => {
  it("requires each suite's complete table set, accepts views/case, and can detect both", () => {
    expect(detectBenchmarks([])).toEqual([]);
    expect(detectBenchmarks(tpch.slice(1))).toEqual([]);
    expect(detectBenchmarks(tpcds.slice(1))).toEqual([]);
    expect(detectBenchmarks(tpch.map((t) => ({ ...t, name: t.name.toUpperCase(), isView: true })))).toEqual([
      benchmarks[0],
    ]);
    expect(detectBenchmarks([...tpch, ...tpcds])).toEqual(benchmarks);
  });

  it("loads all 121 examples and rejects query numbers outside the suite", async () => {
    for (const suite of benchmarks) {
      const queries = await Promise.all(Array.from({ length: suite.count }, (_, i) => benchmarkQuery(suite.id, i + 1)));
      expect(new Set(queries).size).toBe(suite.count);
      for (const sql of queries) expect(sql).toMatch(/;\n$/);
      await expect(benchmarkQuery(suite.id, 0)).rejects.toThrow();
      await expect(benchmarkQuery(suite.id, suite.count + 1)).rejects.toThrow();
    }
    expect(await benchmarkQuery("tpch", 7)).toContain("INNER JOIN");
  });

  it("offers both suites and opens a named query in the selected catalog/schema", async () => {
    const listings = { tables: vi.fn().mockResolvedValue([...tpch, ...tpcds]) };
    const onOpen = vi.fn();
    render(
      <BenchmarkQueries
        listings={listings}
        catalog="lake"
        schema="nested.schema"
        version={0}
        active
        disabled={false}
        onOpen={onOpen}
      />,
    );
    const select = await screen.findByLabelText("Benchmark query");
    expect(listings.tables).toHaveBeenCalledWith("lake", "nested.schema");
    expect(select.querySelectorAll("optgroup option")).toHaveLength(121);
    fireEvent.change(select, { target: { value: "tpch:6" } });
    await waitFor(() =>
      expect(onOpen).toHaveBeenCalledWith({
        title: "TPC-H Q6",
        sql: expect.stringContaining("lineitem"),
        catalog: "lake",
        schema: "nested.schema",
      }),
    );
    expect((select as HTMLSelectElement).value).toBe("");
  });

  it("ignores stale metadata on a schema switch and uses new role/server listings", async () => {
    let resolve!: (rows: TableEntry[]) => void;
    const old = {
      tables: vi.fn().mockReturnValue(
        new Promise<TableEntry[]>((done) => {
          resolve = done;
        }),
      ),
    };
    const current = { tables: vi.fn().mockResolvedValue([]) };
    const props = { catalog: "lake", schema: "old", version: 0, active: true, disabled: false, onOpen: vi.fn() };
    const view = render(<BenchmarkQueries {...props} listings={old} />);
    view.rerender(<BenchmarkQueries {...props} schema="new" listings={current} />);
    await act(async () => {
      resolve(tpch);
    });
    expect(screen.queryByLabelText("Benchmark query")).toBeNull();
    current.tables.mockResolvedValue(tpcds);
    view.rerender(<BenchmarkQueries {...props} schema="new" listings={current} version={1} />);
    expect((await screen.findByLabelText("Benchmark query")).querySelectorAll("optgroup option")).toHaveLength(99);
    current.tables.mockResolvedValue([]);
    view.rerender(<BenchmarkQueries {...props} schema="new" listings={current} version={2} />);
    await waitFor(() => expect(screen.queryByLabelText("Benchmark query")).toBeNull());
  });

  it("does not fetch for hidden tabs, tolerates denied metadata, and disables at the tab limit", async () => {
    const listings = { tables: vi.fn().mockRejectedValue(new Error("Access denied")) };
    const props = { listings, catalog: "lake", schema: "main", version: 0, disabled: false, onOpen: vi.fn() };
    const view = render(<BenchmarkQueries {...props} active={false} />);
    expect(listings.tables).not.toHaveBeenCalled();
    view.rerender(<BenchmarkQueries {...props} active />);
    await act(async () => {});
    expect(screen.queryByLabelText("Benchmark query")).toBeNull();
    listings.tables.mockResolvedValue(tpch);
    view.rerender(<BenchmarkQueries {...props} active version={1} disabled />);
    expect((await screen.findByLabelText<HTMLSelectElement>("Benchmark query")).disabled).toBe(true);
  });
});
