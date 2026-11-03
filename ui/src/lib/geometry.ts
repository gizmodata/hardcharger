// GEOMETRY values: Hardcharger sends a GEOMETRY column as Arrow binary tagged geoarrow.wkb
// (GeoArrow), each value its Well-Known Binary, and the column's CRS (when it has one) as
// PROJJSON in the extension metadata. This decodes WKB (ISO and EWKB, either byte order, every
// dimension) into a small tree, writes it as WKT exactly as DuckDB's ST_AsText does, and as
// GeoJSON for the map. https://geoarrow.org/extension-types.html
import type { Field } from "apache-arrow";

export type Dims = "" | "Z" | "M" | "ZM";

/** A decoded geometry. Coordinates are [x, y, (z), (m)]; an empty point has none. */
export type Geometry =
  | { type: "Point"; dims: Dims; coords: number[] | null }
  | { type: "LineString"; dims: Dims; coords: number[][] }
  | { type: "Polygon"; dims: Dims; coords: number[][][] }
  | { type: "MultiPoint" | "MultiLineString" | "MultiPolygon" | "GeometryCollection"; dims: Dims; parts: Geometry[] };

const wkbExtensions = new Set(["geoarrow.wkb", "ogc.wkb"]);

/** Whether a field holds WKB geometries (geoarrow.wkb, or ogc.wkb from older GeoArrow writers). */
export function isGeometry(field: Field): boolean {
  return wkbExtensions.has(field.metadata.get("ARROW:extension:name") ?? "");
}

/** What a column's CRS says about its coordinates, for the map. */
export interface Crs {
  /** "EPSG:4326", "OGC:CRS84", a CRS's name…, or undefined when the column has none. */
  label: string | undefined;
  /** longitude/latitude in degrees: yes, no, or not known (no CRS, or one this can't read). */
  geographic: boolean | undefined;
}

/** The CRS in a geoarrow.wkb field's metadata (PROJJSON, an "AUTH:CODE" string, or none). */
export function geometryCrs(field: Field): Crs {
  let meta: { crs?: unknown };
  try {
    meta = JSON.parse(field.metadata.get("ARROW:extension:metadata") || "{}") as { crs?: unknown };
  } catch {
    return { label: undefined, geographic: undefined };
  }
  const crs = meta.crs;
  if (crs === undefined || crs === null || crs === "") return { label: undefined, geographic: undefined };
  if (typeof crs === "string") {
    const code = crs.trim().toUpperCase();
    const geographic = ["EPSG:4326", "OGC:CRS84", "CRS84", "EPSG:4269", "EPSG:4258", "EPSG:4979"].includes(code);
    return { label: crs, geographic: geographic ? true : code === "EPSG:3857" ? false : undefined };
  }
  if (typeof crs === "object") {
    const p = crs as { type?: string; name?: string; id?: { authority?: string; code?: number | string } };
    const label = p.id?.authority && p.id.code !== undefined ? `${p.id.authority}:${p.id.code}` : p.name;
    // A geographic CRS (GeographicCRS, or GeodeticCRS with an ellipsoidal system) is in degrees.
    const geographic = p.type === "GeographicCRS" ? true : p.type === "ProjectedCRS" ? false : undefined;
    return { label, geographic };
  }
  return { label: undefined, geographic: undefined };
}

class WkbReader {
  private at = 0;
  private readonly view: DataView;
  constructor(readonly bytes: Uint8Array) {
    this.view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  }
  get done() {
    return this.at === this.bytes.length;
  }
  private need(n: number) {
    if (this.at + n > this.bytes.length) throw new Error("WKB ends early");
  }
  byte(): number {
    this.need(1);
    return this.view.getUint8(this.at++);
  }
  uint(little: boolean): number {
    this.need(4);
    const v = this.view.getUint32(this.at, little);
    this.at += 4;
    return v;
  }
  double(little: boolean): number {
    this.need(8);
    const v = this.view.getFloat64(this.at, little);
    this.at += 8;
    return v;
  }
  /** A count, checked against the bytes left (each item takes at least `minBytes`). */
  count(little: boolean, minBytes: number): number {
    const n = this.uint(little);
    if (n * minBytes > this.bytes.length - this.at) throw new Error("WKB count past the end");
    return n;
  }
}

const typeNames = ["", "Point", "LineString", "Polygon", "MultiPoint", "MultiLineString", "MultiPolygon"] as const;
const maxDepth = 64;

function readGeometry(r: WkbReader, depth: number): Geometry {
  if (depth > maxDepth) throw new Error("WKB nested too deep");
  const order = r.byte();
  if (order > 1) throw new Error("not WKB (byte order)");
  const little = order === 1;
  let code = r.uint(little);
  // EWKB (PostGIS): flags in the high bits, and an SRID after the type when 0x20000000 is set.
  let z = (code & 0x80000000) !== 0;
  let m = (code & 0x40000000) !== 0;
  if (code & 0x20000000) r.uint(little);
  code &= 0x0fffffff;
  // ISO: 1000s for Z, 2000s for M, 3000s for ZM.
  const iso = Math.floor(code / 1000);
  if (iso > 3) throw new Error(`not WKB (type ${code})`);
  if (iso === 1 || iso === 3) z = true;
  if (iso === 2 || iso === 3) m = true;
  const kind = code % 1000;
  const dims: Dims = z && m ? "ZM" : z ? "Z" : m ? "M" : "";
  const width = 2 + (z ? 1 : 0) + (m ? 1 : 0);
  const point = () => {
    const c: number[] = [];
    for (let i = 0; i < width; i++) c.push(r.double(little));
    return c;
  };
  const points = () => {
    const n = r.count(little, width * 8);
    const out: number[][] = [];
    for (let i = 0; i < n; i++) out.push(point());
    return out;
  };
  switch (kind) {
    case 1: {
      const c = point();
      // An empty point is written with NaN coordinates.
      return { type: "Point", dims, coords: c.every((v) => Number.isNaN(v)) ? null : c };
    }
    case 2:
      return { type: "LineString", dims, coords: points() };
    case 3: {
      const n = r.count(little, 4);
      const rings: number[][][] = [];
      for (let i = 0; i < n; i++) rings.push(points());
      return { type: "Polygon", dims, coords: rings };
    }
    case 4:
    case 5:
    case 6:
    case 7: {
      const n = r.count(little, 5);
      const parts: Geometry[] = [];
      for (let i = 0; i < n; i++) parts.push(readGeometry(r, depth + 1));
      const type = kind === 7 ? "GeometryCollection" : typeNames[kind];
      if (kind !== 7 && parts.some((p) => p.type !== typeNames[kind - 3])) throw new Error(`a ${type} of other types`);
      return { type, dims, parts };
    }
    default:
      throw new Error(`unknown WKB geometry type ${code}`);
  }
}

/** Decodes one WKB value; throws on anything that isn't exactly one well-formed geometry. */
export function parseWkb(bytes: Uint8Array): Geometry {
  const r = new WkbReader(bytes);
  const g = readGeometry(r, 0);
  if (!r.done) throw new Error("bytes after the WKB geometry");
  return g;
}

/**
 * A coordinate as DuckDB prints it: the shortest text that reads back as the same double,
 * with an exponent (at least two digits) below 1e-4 or from 1e16 up: 1e-05, 1e+16, -0.
 */
export function coordText(v: number): string {
  if (Number.isNaN(v)) return "nan";
  if (!Number.isFinite(v)) return v > 0 ? "inf" : "-inf";
  if (v === 0) return Object.is(v, -0) ? "-0" : "0";
  const [mantissa, exp] = v.toExponential().split("e") as [string, string];
  const e = Number(exp);
  if (e < -4 || e >= 16) return `${mantissa}e${e < 0 ? "-" : "+"}${String(Math.abs(e)).padStart(2, "0")}`;
  // Fixed notation from the shortest digits (toString would switch to exponents at 1e21).
  const negative = mantissa.startsWith("-");
  const digits = mantissa.replace("-", "").replace(".", "");
  let text: string;
  if (e < 0) text = `0.${"0".repeat(-e - 1)}${digits}`;
  else if (digits.length <= e + 1) text = digits + "0".repeat(e + 1 - digits.length);
  else text = `${digits.slice(0, e + 1)}.${digits.slice(e + 1)}`;
  return negative ? `-${text}` : text;
}

const wktName: Record<Geometry["type"], string> = {
  Point: "POINT",
  LineString: "LINESTRING",
  Polygon: "POLYGON",
  MultiPoint: "MULTIPOINT",
  MultiLineString: "MULTILINESTRING",
  MultiPolygon: "MULTIPOLYGON",
  GeometryCollection: "GEOMETRYCOLLECTION",
};

const coordsText = (c: number[]) => c.map(coordText).join(" ");
const listText = (cs: number[][]) => (cs.length ? `(${cs.map(coordsText).join(", ")})` : "EMPTY");

/** The geometry's body: what follows its tag ("(1 2)", "EMPTY"…). */
function body(g: Geometry): string {
  switch (g.type) {
    case "Point":
      return g.coords ? `(${coordsText(g.coords)})` : "EMPTY";
    case "LineString":
      return listText(g.coords);
    case "Polygon":
      return g.coords.length ? `(${g.coords.map(listText).join(", ")})` : "EMPTY";
    case "MultiPoint":
      // DuckDB writes MULTIPOINT (1 2, 3 4), without parentheses around each point.
      return g.parts.length
        ? `(${g.parts.map((p) => (p.type === "Point" && p.coords ? coordsText(p.coords) : "EMPTY")).join(", ")})`
        : "EMPTY";
    case "MultiLineString":
    case "MultiPolygon":
      return g.parts.length ? `(${g.parts.map(body).join(", ")})` : "EMPTY";
    case "GeometryCollection":
      return g.parts.length ? `(${g.parts.map(toWkt).join(", ")})` : "EMPTY";
  }
}

/** Well-Known Text, as DuckDB's ST_AsText writes it. */
export function toWkt(g: Geometry): string {
  return `${wktName[g.type]}${g.dims ? ` ${g.dims}` : ""} ${body(g)}`;
}

/** A GeoJSON geometry (x, y and z; GeoJSON has no M), or null for an empty one. */
export type GeoJsonGeometry =
  | { type: "Point"; coordinates: number[] }
  | { type: "LineString" | "MultiPoint"; coordinates: number[][] }
  | { type: "Polygon" | "MultiLineString"; coordinates: number[][][] }
  | { type: "MultiPolygon"; coordinates: number[][][][] }
  | { type: "GeometryCollection"; geometries: GeoJsonGeometry[] };

export function toGeoJson(g: Geometry): GeoJsonGeometry | null {
  const xyz = (c: number[]) => (g.dims === "Z" || g.dims === "ZM" ? c.slice(0, 3) : c.slice(0, 2));
  switch (g.type) {
    case "Point":
      return g.coords ? { type: "Point", coordinates: xyz(g.coords) } : null;
    case "LineString":
      return g.coords.length ? { type: "LineString", coordinates: g.coords.map(xyz) } : null;
    case "Polygon": {
      const rings = g.coords.filter((ring) => ring.length > 0);
      return rings.length ? { type: "Polygon", coordinates: rings.map((ring) => ring.map(xyz)) } : null;
    }
    case "MultiPoint": {
      const pts = g.parts.flatMap((p) => (p.type === "Point" && p.coords ? [xyz(p.coords)] : []));
      return pts.length ? { type: "MultiPoint", coordinates: pts } : null;
    }
    case "MultiLineString": {
      const lines = g.parts.flatMap((p) => (p.type === "LineString" && p.coords.length ? [p.coords.map(xyz)] : []));
      return lines.length ? { type: "MultiLineString", coordinates: lines } : null;
    }
    case "MultiPolygon": {
      const polys = g.parts.flatMap((p) => {
        const json = toGeoJson(p);
        return json?.type === "Polygon" ? [json.coordinates] : [];
      });
      return polys.length ? { type: "MultiPolygon", coordinates: polys } : null;
    }
    case "GeometryCollection": {
      const parts = g.parts.map(toGeoJson).filter((p): p is GeoJsonGeometry => p !== null);
      return parts.length ? { type: "GeometryCollection", geometries: parts } : null;
    }
  }
}

/** Every x, y of a geometry, for bounds. */
export function forEachXY(g: Geometry, visit: (x: number, y: number) => void): void {
  switch (g.type) {
    case "Point":
      if (g.coords) visit(g.coords[0]!, g.coords[1]!);
      return;
    case "LineString":
      for (const c of g.coords) visit(c[0]!, c[1]!);
      return;
    case "Polygon":
      for (const ring of g.coords) for (const c of ring) visit(c[0]!, c[1]!);
      return;
    default:
      for (const p of g.parts) forEachXY(p, visit);
  }
}

/** A short description for the map's tooltips: "POINT", "POLYGON · 2 rings", "MULTIPOINT · 3 points". */
export function geometrySummary(g: Geometry): string {
  const name = `${wktName[g.type]}${g.dims ? ` ${g.dims}` : ""}`;
  const n = (k: number, word: string) => `${k.toLocaleString()} ${word}${k === 1 ? "" : "s"}`;
  switch (g.type) {
    case "Point":
      return g.coords ? name : `${name} EMPTY`;
    case "LineString":
      return g.coords.length ? `${name} · ${n(g.coords.length, "point")}` : `${name} EMPTY`;
    case "Polygon":
      return g.coords.length ? `${name} · ${n(g.coords.length, "ring")}` : `${name} EMPTY`;
    case "MultiPoint":
      return `${name} · ${n(g.parts.length, "point")}`;
    case "MultiLineString":
      return `${name} · ${n(g.parts.length, "line")}`;
    case "MultiPolygon":
      return `${name} · ${n(g.parts.length, "polygon")}`;
    case "GeometryCollection":
      return `${name} · ${n(g.parts.length, "part")}`;
  }
}

/** The extent of a set of geometries' x, y (Infinity / -Infinity when there are none). */
export interface Extent {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

export function extentOf(geometries: Iterable<Geometry>): Extent {
  const e = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity };
  for (const g of geometries)
    forEachXY(g, (x, y) => {
      if (x < e.minX) e.minX = x;
      if (x > e.maxX) e.maxX = x;
      if (y < e.minY) e.minY = y;
      if (y > e.maxY) e.maxY = y;
    });
  return e;
}

/**
 * How the map draws a column: on the world basemap (x, y are longitude, latitude), on it after
 * unprojecting Web Mercator (EPSG:3857 metres), or on a plain plane with no basemap (any other
 * projected CRS, or no CRS with coordinates that can't be degrees). `note` says which, for the
 * map's legend.
 */
export function mapSpace(crs: Crs, extent: Extent): { space: "lonlat" | "webmercator" | "plane"; note: string } {
  const label = crs.label;
  if (label === "EPSG:3857" || label === "EPSG:900913")
    return { space: "webmercator", note: `${label} (Web Mercator), shown on the world map` };
  if (crs.geographic === true) return { space: "lonlat", note: `${label ?? "Geographic CRS"}: longitude, latitude` };
  const empty = extent.minX > extent.maxX;
  const degrees = empty || (extent.minX >= -180 && extent.maxX <= 180 && extent.minY >= -90 && extent.maxY <= 90);
  if (crs.geographic === undefined && label === undefined && degrees)
    return { space: "lonlat", note: "No CRS on the column: drawn as longitude, latitude" };
  return {
    space: "plane",
    note: label
      ? `${label}: drawn as plain x, y (no basemap for this CRS)`
      : "No CRS, and the coordinates aren't degrees: drawn as plain x, y",
  };
}

/**
 * A line or ring of [lon, lat] with no jump of more than 180° between neighbours: a shape that
 * crosses the antimeridian (Fiji, Chukotka) goes on past ±180 instead of drawing a sliver back
 * across the whole world. Points are shifted by whole turns (360°), never moved otherwise.
 */
export function unwrapLongitudes(line: number[][]): number[][] {
  const out: number[][] = [];
  let shift = 0;
  let last = 0;
  for (const c of line) {
    let lon = c[0]! + shift;
    if (out.length > 0) {
      while (lon - last > 180) {
        shift -= 360;
        lon -= 360;
      }
      while (lon - last < -180) {
        shift += 360;
        lon += 360;
      }
    }
    out.push(shift ? [lon, ...c.slice(1)] : c);
    last = lon;
  }
  return out;
}
