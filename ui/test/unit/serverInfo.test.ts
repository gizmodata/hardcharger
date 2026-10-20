import { describe, expect, it } from "vitest";

import { remainingServerInfo } from "../../src/lib/serverInfo";

describe("remainingServerInfo", () => {
  it("leaves out what About's summary shows, keeps the rest in the server's order", () => {
    const rows = remainingServerInfo({
      server_name: "Hardcharger",
      server_version: "2.0.0",
      protocol: "warp-sql",
      protocol_version: "1",
      instance_id: "i",
      hardcharger_instance_id: "i",
      duckdb_version: "v2.0.0",
      sql_transactions_supported: true,
      server_arrow_version: "26.0.0",
      nested: { a: 1 },
    });
    expect(rows).toEqual([
      ["duckdb_version", "v2.0.0"],
      ["sql_transactions_supported", "true"],
      ["server_arrow_version", "26.0.0"],
    ]);
  });
});
