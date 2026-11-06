// Fixed examples from the pinned DuckDB checkout; provenance and regeneration instructions
// live in src/benchmarks/README.md. SQL is loaded only when an example is selected.
export const benchmarks = [
  {
    id: "tpch",
    name: "TPC-H",
    count: 22,
    tables: ["region", "nation", "supplier", "customer", "part", "partsupp", "orders", "lineitem"],
  },
  {
    id: "tpcds",
    name: "TPC-DS",
    count: 99,
    tables: [
      "call_center",
      "catalog_page",
      "catalog_returns",
      "catalog_sales",
      "customer",
      "customer_address",
      "customer_demographics",
      "date_dim",
      "household_demographics",
      "income_band",
      "inventory",
      "item",
      "promotion",
      "reason",
      "ship_mode",
      "store",
      "store_returns",
      "store_sales",
      "time_dim",
      "warehouse",
      "web_page",
      "web_returns",
      "web_sales",
      "web_site",
    ],
  },
] as const;
export type Benchmark = (typeof benchmarks)[number];

/** All required names must be visible in this schema. Views can expose benchmark data too. */
export function detectBenchmarks(tables: readonly { name: string }[]): Benchmark[] {
  const names = new Set(tables.map((table) => table.name.toLowerCase()));
  return benchmarks.filter((benchmark) => benchmark.tables.every((name) => names.has(name)));
}

export async function benchmarkQuery(id: Benchmark["id"], number: number): Promise<string> {
  const benchmark = benchmarks.find((b) => b.id === id);
  if (!benchmark || !Number.isInteger(number) || number < 1 || number > benchmark.count)
    throw new Error("Unknown benchmark query");
  const queries = id === "tpch" ? await import("../benchmarks/tpch.json") : await import("../benchmarks/tpcds.json");
  return queries.default[number - 1]!;
}
