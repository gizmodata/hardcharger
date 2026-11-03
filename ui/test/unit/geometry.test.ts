import { Field, Binary, tableFromIPC } from "apache-arrow";
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { formatBinary, jsonFor, textFor, typeLabel } from "../../src/lib/format";
import {
  coordText,
  extentOf,
  geometryCrs,
  geometrySummary,
  isGeometry,
  mapSpace,
  parseWkb,
  toGeoJson,
  toWkt,
  unwrapLongitudes,
} from "../../src/lib/geometry";

// GEOMETRY values as a Hardcharger server sends them, with DuckDB's ST_AsText of each
// (test/unit/fixtures/geometry.arrows, written by `node test/unit/fixtures/make-geometry.mjs`).
const table = tableFromIPC(readFileSync(path.join(import.meta.dirname, "fixtures", "geometry.arrows")));
const field = (name: string) => table.schema.fields.find((f) => f.name === name)!;
const column = (name: string) => table.getChild(name)!;
const rows = Array.from({ length: table.numRows }, (_, i) => ({
  input: column("input").get(i) as string,
  text: column("text").get(i) as string,
  wkb: column("g").get(i) as Uint8Array,
}));

const wkbField = (metadata: Record<string, string> = {}) =>
  new Field(
    "g",
    new Binary(),
    true,
    new Map(Object.entries({ "ARROW:extension:name": "geoarrow.wkb", "ARROW:extension:metadata": "{}", ...metadata })),
  );

/** Little-endian WKB of a 2D point (type 1) or any other header + doubles. */
function wkb(code: number, doubles: number[], { little = true, counts = [] as number[] } = {}): Uint8Array {
  const out = new DataView(new ArrayBuffer(5 + counts.length * 4 + doubles.length * 8));
  out.setUint8(0, little ? 1 : 0);
  out.setUint32(1, code, little);
  let at = 5;
  for (const c of counts) {
    out.setUint32(at, c, little);
    at += 4;
  }
  for (const d of doubles) {
    out.setFloat64(at, d, little);
    at += 8;
  }
  return new Uint8Array(out.buffer);
}

describe("GEOMETRY as text", () => {
  it("has a fixture covering every type, dimension and empty case", () => {
    expect(rows.length).toBeGreaterThanOrEqual(30);
    for (const word of ["POINT ZM", "POLYGON EMPTY", "MULTIPOINT", "MULTILINESTRING", "MULTIPOLYGON", "COLLECTION M"])
      expect(rows.some((r) => r.text.includes(word))).toBe(true);
  });

  it.each(rows)("writes $input exactly as DuckDB's ST_AsText does", ({ wkb: bytes, text }) => {
    expect(toWkt(parseWkb(bytes))).toBe(text);
  });

  it("shows WKT for geometry columns, at the top level and nested, and keeps NULL", () => {
    const g = textFor(field("g"));
    const crs = textFor(field("g4326"));
    const list = textFor(field("list"));
    const st = textFor(field("st"));
    rows.forEach((r, i) => {
      expect(g(column("g").get(i))).toBe(r.text);
      expect(crs(column("g4326").get(i))).toBe(r.text);
      expect(list(column("list").get(i))).toBe(`[${r.text}, NULL]`);
      expect(st(column("st").get(i))).toBe(`{'g': ${r.text}}`);
    });
    expect(textFor(field("n"))(column("n").get(0))).toBeNull();
  });

  it("writes WKT strings into JSON", () => {
    expect(jsonFor(field("g"))(column("g").get(0))).toBe(JSON.stringify(rows[0]!.text));
    expect(jsonFor(field("n"))(null)).toBe("null");
  });

  it("names the column type as DuckDB's typeof does", () => {
    expect(typeLabel(field("g"))).toBe("GEOMETRY");
    expect(typeLabel(field("g4326"))).toBe("GEOMETRY('EPSG:4326')");
    expect(typeLabel(field("list"))).toBe("GEOMETRY[]");
  });

  it("leaves plain BLOBs alone: only the geoarrow.wkb (or ogc.wkb) extension decodes", () => {
    const plain = new Field("b", new Binary(), true);
    expect(isGeometry(plain)).toBe(false);
    expect(textFor(plain)(rows[0]!.wkb)).toBe(formatBinary(rows[0]!.wkb));
    expect(typeLabel(plain)).toBe("BLOB");
    expect(isGeometry(wkbField({ "ARROW:extension:name": "ogc.wkb" }))).toBe(true);
  });
});

describe("WKB decoding", () => {
  it("reads big-endian WKB", () => {
    expect(toWkt(parseWkb(wkb(1, [1.5, -2], { little: false })))).toBe("POINT (1.5 -2)");
    expect(toWkt(parseWkb(wkb(1002, [0, 0, 1, 1, 1, 2], { little: false, counts: [2] })))).toBe(
      "LINESTRING Z (0 0 1, 1 1 2)",
    );
  });

  it("reads PostGIS EWKB: Z and M flags, and an SRID", () => {
    // Z flag 0x80000000 | SRID flag 0x20000000, then the SRID (4326) where a count would be.
    expect(toWkt(parseWkb(wkb(0xa0000001, [1, 2, 3], { counts: [4326] })))).toBe("POINT Z (1 2 3)");
    expect(toWkt(parseWkb(wkb(0x40000001, [1, 2, 3])))).toBe("POINT M (1 2 3)");
  });

  it("refuses what isn't one whole WKB geometry, and the grid then shows the bytes", () => {
    const point = wkb(1, [1, 2]);
    const bad = [
      point.subarray(0, 12), // cut short
      new Uint8Array([...point, 0]), // a byte too many
      wkb(1, [1, 2], { little: true }).map((b, i) => (i === 0 ? 7 : b)), // not a byte order
      wkb(99, [1, 2]), // no such type
      wkb(4, [], { counts: [0xffffffff] }), // a count far past the end: refused before any allocation
      wkb(4, [1, 2], { counts: [1] }).map((b, i) => (i === 10 ? 2 : b)), // a multipoint holding a linestring
    ];
    for (const bytes of bad) {
      expect(() => parseWkb(bytes)).toThrow();
      expect(textFor(wkbField())(bytes)).toBe(formatBinary(bytes));
    }
  });

  it("refuses collections nested deeper than any real geometry, without overflowing the stack", () => {
    const head = (n: number) => [1, 7, 0, 0, 0, 1, 0, 0, 0].slice(0, n);
    const bomb = new Uint8Array([...Array.from({ length: 10_000 }, () => head(9)).flat(), ...wkb(1, [1, 2])]);
    expect(() => parseWkb(bomb)).toThrow(/too deep/);
  });
});

describe("coordinates", () => {
  it.each([
    [0, "0"],
    [-0, "-0"],
    [1, "1"],
    [0.1, "0.1"],
    [0.0001, "0.0001"],
    [0.00001, "1e-05"],
    [1e15, "1000000000000000"],
    [1e16, "1e+16"],
    [1.5e300, "1.5e+300"],
    [-2.5e-300, "-2.5e-300"],
    [123456789.12345679, "123456789.12345679"],
    [-80.8431, "-80.8431"],
    [Number.NaN, "nan"],
    [Number.POSITIVE_INFINITY, "inf"],
  ])("%s prints as %s", (v, text) => {
    expect(coordText(v)).toBe(text);
  });
});

describe("GeoJSON and CRS for the map", () => {
  it("drops M, keeps Z, and maps empties to null", () => {
    const geo = rows.map((r) => ({ text: r.text, json: toGeoJson(parseWkb(r.wkb)) }));
    const of = (text: string) => geo.find((g) => g.text === text)!.json;
    expect(of("POINT (1 2)")).toEqual({ type: "Point", coordinates: [1, 2] });
    expect(of("POINT M (1 2 3)")).toEqual({ type: "Point", coordinates: [1, 2] });
    expect(of("POINT ZM (1 2 3 4)")).toEqual({ type: "Point", coordinates: [1, 2, 3] });
    expect(of("MULTILINESTRING (EMPTY, (0 0, 1 1))")).toEqual({
      type: "MultiLineString",
      coordinates: [
        [
          [0, 0],
          [1, 1],
        ],
      ],
    });
    for (const g of geo.filter((x) => x.text.endsWith("EMPTY"))) expect(g.json).toBeNull();
    expect(of("GEOMETRYCOLLECTION (POINT EMPTY, GEOMETRYCOLLECTION (MULTIPOINT (1 1), LINESTRING EMPTY))")).toEqual({
      type: "GeometryCollection",
      geometries: [{ type: "GeometryCollection", geometries: [{ type: "MultiPoint", coordinates: [[1, 1]] }] }],
    });
  });

  it("reads the column's CRS from its PROJJSON", () => {
    expect(geometryCrs(field("g"))).toEqual({ label: undefined, geographic: undefined });
    expect(geometryCrs(field("g4326"))).toEqual({ label: "EPSG:4326", geographic: true });
    expect(geometryCrs(wkbField({ "ARROW:extension:metadata": '{"crs":"OGC:CRS84"}' }))).toEqual({
      label: "OGC:CRS84",
      geographic: true,
    });
    const projected = JSON.stringify({
      crs: { type: "ProjectedCRS", name: "x", id: { authority: "EPSG", code: 32617 } },
    });
    expect(geometryCrs(wkbField({ "ARROW:extension:metadata": projected }))).toEqual({
      label: "EPSG:32617",
      geographic: false,
    });
    expect(geometryCrs(wkbField({ "ARROW:extension:metadata": "not json" })).label).toBeUndefined();
  });

  it("summarizes geometries for tooltips", () => {
    expect(geometrySummary(parseWkb(rows.find((r) => r.text.startsWith("POLYGON ((0 0, 10"))!.wkb))).toBe(
      "POLYGON · 2 rings",
    );
  });
});

describe("where the map draws a column", () => {
  const extent = (wkt: string[]) => extentOf(wkt.map((t) => parseWkb(rows.find((r) => r.text === t)!.wkb)));
  const degrees = extent(["POINT (-80.8431 35.2271)", "POINT (1 2)"]);
  const none = { label: undefined, geographic: undefined };

  it("puts lon/lat on the world map: a geographic CRS, or no CRS with coordinates that fit", () => {
    expect(mapSpace({ label: "EPSG:4326", geographic: true }, degrees).space).toBe("lonlat");
    expect(mapSpace(none, degrees)).toEqual({
      space: "lonlat",
      note: "No CRS on the column: drawn as longitude, latitude",
    });
    expect(mapSpace(none, extentOf([])).space).toBe("lonlat");
  });

  it("unprojects Web Mercator, and draws any other projected CRS (or out-of-range x, y) on a plane", () => {
    expect(mapSpace({ label: "EPSG:3857", geographic: false }, degrees).space).toBe("webmercator");
    const utm = { minX: 514000, minY: 3898000, maxX: 515000, maxY: 3899000 };
    expect(mapSpace({ label: "EPSG:32617", geographic: false }, utm)).toEqual({
      space: "plane",
      note: "EPSG:32617: drawn as plain x, y (no basemap for this CRS)",
    });
    expect(mapSpace(none, utm).space).toBe("plane");
    // A projected CRS whose numbers happen to look like degrees is still not degrees.
    expect(mapSpace({ label: "EPSG:32617", geographic: false }, degrees).space).toBe("plane");
  });
});

describe("antimeridian", () => {
  it("unwraps a ring that crosses ±180 instead of letting it span the world", () => {
    // Fiji-like: 179 → -179 is 2° east, not 358° west.
    expect(
      unwrapLongitudes([
        [178, -17],
        [179.5, -17],
        [-179.5, -16],
        [-179, -17],
        [178, -17],
      ]),
    ).toEqual([
      [178, -17],
      [179.5, -17],
      [180.5, -16],
      [181, -17],
      [178, -17],
    ]);
    expect(
      unwrapLongitudes([
        [-179, 65],
        [179, 66],
      ]),
    ).toEqual([
      [-179, 65],
      [-181, 66],
    ]);
    const plain = [
      [0, 0],
      [10, 10],
      [-170, 5],
    ];
    expect(unwrapLongitudes(plain)).toEqual(plain);
  });
});
