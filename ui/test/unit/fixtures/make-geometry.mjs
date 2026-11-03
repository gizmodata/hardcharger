// Writes geometry.arrows: GEOMETRY values as a Hardcharger server sends them (geoarrow.wkb) next
// to DuckDB's own text for each (ST_AsText), for the geometry tests. Run against a server:
//   HARDCHARGER_UI_TEST_URL=http://127.0.0.1:31337 HARDCHARGER_UI_TEST_USERNAME=admin \
//   HARDCHARGER_UI_TEST_PASSWORD=… node test/unit/fixtures/make-geometry.mjs
import { writeFileSync } from "node:fs";

import { connect } from "@gizmodata/chargewire";
import { tableToIPC } from "apache-arrow";

// Every geometry type, each dimension (XY, Z, M, ZM), empties at every level, and numbers
// that DuckDB prints in its own style (exponents, long fractions, negative zero).
const cases = [
  "POINT (1 2)",
  "POINT (-80.8431 35.2271)",
  "POINT (1e20 -1e-7)",
  "POINT (1e15 1e16)",
  "POINT (123456789012345678 0.0001)",
  "POINT (0.00001 -0.5)",
  "POINT (-0 123456789.123456789)",
  "POINT (0.1 0.30000000000000004)",
  "POINT (1.5e300 -2.5e-300)",
  "POINT (179.99999999999997 -89.123456789012345)",
  "POINT EMPTY",
  "POINT Z (1 2 3)",
  "POINT M (1 2 3)",
  "POINT ZM (1 2 3 4)",
  "POINT Z EMPTY",
  "LINESTRING (0 0, 1 1, 2 0.5)",
  "LINESTRING Z (0 0 1, 1 1 2)",
  "LINESTRING EMPTY",
  "POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0), (2 2, 2 4, 4 4, 4 2, 2 2))",
  "POLYGON ZM ((0 0 1 2, 1 0 1 2, 1 1 1 2, 0 0 1 2))",
  "POLYGON EMPTY",
  "MULTIPOINT ((1 2), (3 4))",
  "MULTIPOINT Z ((1 2 3), (4 5 6))",
  "MULTIPOINT EMPTY",
  "MULTILINESTRING ((0 0, 1 1), (2 2, 3 3, 4 4))",
  "MULTILINESTRING (EMPTY, (0 0, 1 1))",
  "MULTILINESTRING EMPTY",
  "MULTIPOLYGON (((0 0, 1 0, 1 1, 0 0)), ((5 5, 6 5, 6 6, 5 5), (5.2 5.1, 5.8 5.1, 5.8 5.7, 5.2 5.1)))",
  "MULTIPOLYGON (EMPTY, ((0 0, 1 0, 1 1, 0 0)))",
  "MULTIPOLYGON EMPTY",
  "GEOMETRYCOLLECTION (POINT (1 2), LINESTRING (0 0, 1 1), POLYGON ((0 0, 1 0, 1 1, 0 0)))",
  "GEOMETRYCOLLECTION (POINT EMPTY, GEOMETRYCOLLECTION (MULTIPOINT ((1 1)), LINESTRING EMPTY))",
  "GEOMETRYCOLLECTION M (POINT M (1 2 3))",
  "GEOMETRYCOLLECTION EMPTY",
];

const conn = await connect({
  url: process.env.HARDCHARGER_UI_TEST_URL,
  username: process.env.HARDCHARGER_UI_TEST_USERNAME,
  password: process.env.HARDCHARGER_UI_TEST_PASSWORD,
});
await conn.execute("LOAD spatial");
const values = cases.map((_, i) => `(${i}, ?)`).join(", ");
const table = await conn.query(
  `SELECT i, input, g, ST_AsText(g) AS text, g::GEOMETRY('EPSG:4326') AS g4326, [g, NULL] AS list,
     {'g': g} AS st, NULL::GEOMETRY AS n
   FROM (SELECT i, input, input::GEOMETRY AS g FROM (VALUES ${values}) v(i, input)) ORDER BY i`,
  cases,
);
writeFileSync(new URL("geometry.arrows", import.meta.url), tableToIPC(table, "stream"));
await conn.close();
console.log(`geometry.arrows: ${table.numRows} geometries`);
