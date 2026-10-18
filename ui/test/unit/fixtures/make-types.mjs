// Writes types.arrows: one row of many DuckDB types as a Hardcharger server sends them, for
// the formatting tests. Run against a server:
//   HARDCHARGER_UI_TEST_URL=http://127.0.0.1:31337 HARDCHARGER_UI_TEST_USERNAME=admin \
//   HARDCHARGER_UI_TEST_PASSWORD=… node test/unit/fixtures/make-types.mjs
import { writeFileSync } from "node:fs";

import { connect } from "@gizmodata/warp-sql";
import { tableToIPC } from "apache-arrow";

const conn = await connect({
  url: process.env.HARDCHARGER_UI_TEST_URL,
  username: process.env.HARDCHARGER_UI_TEST_USERNAME,
  password: process.env.HARDCHARGER_UI_TEST_PASSWORD,
});
await conn.execute("SET TimeZone = 'UTC'");
const table = await conn.query(`SELECT
  1::TINYINT AS tiny, 9007199254740993::BIGINT AS big, 3.5::DOUBLE AS dbl,
  (-12.345)::DECIMAL(10,3) AS dec, 0.05::DECIMAL(4,2) AS dec_small,
  123456789012345678901234567890::HUGEINT AS huge,
  DATE '2026-10-18' AS d, TIMESTAMP '2026-10-18 01:02:03.456789' AS ts,
  TIMESTAMP '2026-10-18 01:02:03' AS ts_whole, TIMESTAMPTZ '2026-10-18 01:02:03+00' AS tz,
  TIME '01:02:03.5' AS t, INTERVAL '1 year 2 months 3 days 4 hours 5 minutes 6 seconds' AS iv,
  [1, 2, NULL] AS l, {'k': 1, 'v': 'z'} AS st, MAP {'a': 1} AS m, 'A\\x00\\xFF'::BLOB AS bl,
  'b'::ENUM('a', 'b') AS e, NULL::INTEGER AS n, true AS bo`);
writeFileSync(new URL("types.arrows", import.meta.url), tableToIPC(table, "stream"));
await conn.close();
console.log(`types.arrows: ${table.numCols} columns`);
