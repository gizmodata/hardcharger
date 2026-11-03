// Maps of GEOMETRY values: the results' Map view (every row shown, filtered and sorted as the
// grid has them) and the cell inspector's preview of one value. Loaded only when a map opens
// (React.lazy): Leaflet, the drawing code and the basemap stay out of the app's first load.
//
// Nothing comes from another origin (the server's Content-Security-Policy allows none): the
// basemap is Natural Earth's land and borders (public domain, via world-atlas), files of the
// UI's own, 1:110m for the world and 1:50m once zoomed in. The basemap is SVG styled by the
// app's CSS (so it follows the theme); the data is drawn on a canvas, which keeps thousands of
// features fast.
import "leaflet/dist/leaflet.css";

import type { Table } from "apache-arrow";
import L from "leaflet";
import { useEffect, useMemo, useRef, useState } from "react";
import { feature, mesh } from "topojson-client";
import type { GeometryCollection, Topology } from "topojson-specification";
import countries110 from "world-atlas/countries-110m.json?url";
import countries50 from "world-atlas/countries-50m.json?url";

import { columnText, typeLabel } from "../lib/format";
import {
  type Crs,
  type Geometry,
  extentOf,
  geometryCrs,
  geometrySummary,
  mapSpace,
  parseWkb,
  toGeoJson,
  unwrapLongitudes,
} from "../lib/geometry";

/** More features than this are not drawn (the map says so): the canvas would crawl. */
const maxFeatures = 100_000;
/** From this zoom on, the 1:50m basemap replaces the 1:110m one. */
const detailZoom = 3.5;
/** A popup shows at most this many characters of a value. */
const popupValueCap = 160;

interface Item {
  geometry: Geometry;
  /** "Row 12": the row's number as the grid shows it. */
  label: string;
  /** The row's values, for its popup (name, type, text). */
  details?: () => [string, string, string | null][];
}

// ── The basemap: fetched once per scale, shared by every map ─────────────────────────────────
/** Natural Earth is drawn for a sphere: on a flat map, lines across the antimeridian unwrap. */
function unwrapShapes(g: GeoJSON.Geometry): GeoJSON.Geometry {
  switch (g.type) {
    case "LineString":
      return { ...g, coordinates: unwrapLongitudes(g.coordinates) };
    case "MultiLineString":
    case "Polygon":
      return { ...g, coordinates: g.coordinates.map(unwrapLongitudes) };
    case "MultiPolygon":
      return { ...g, coordinates: g.coordinates.map((p) => p.map(unwrapLongitudes)) };
    case "GeometryCollection":
      return { ...g, geometries: g.geometries.map(unwrapShapes) };
    default:
      return g;
  }
}
function unwrapObject(o: GeoJSON.GeoJsonObject): GeoJSON.GeoJsonObject {
  if (o.type === "FeatureCollection") {
    const fc = o as GeoJSON.FeatureCollection;
    const out: GeoJSON.FeatureCollection = {
      ...fc,
      features: fc.features.map((f) => ({ ...f, geometry: unwrapShapes(f.geometry) })),
    };
    return out;
  }
  if (o.type === "Feature") {
    const f = o as GeoJSON.Feature;
    const out: GeoJSON.Feature = { ...f, geometry: unwrapShapes(f.geometry) };
    return out;
  }
  return unwrapShapes(o as GeoJSON.Geometry);
}

type Basemap = { land: GeoJSON.GeoJsonObject; borders: GeoJSON.GeoJsonObject };
const basemaps = new Map<string, Promise<Basemap>>();
function loadBasemap(url: string): Promise<Basemap> {
  let p = basemaps.get(url);
  if (!p) {
    p = fetch(url)
      .then((r) => {
        if (!r.ok) throw new Error(`basemap: HTTP ${r.status}`);
        return r.json() as Promise<Topology>;
      })
      .then((topo) => {
        const countries = topo.objects["countries"] as GeometryCollection;
        return {
          land: unwrapObject(feature(topo, topo.objects["land"] as GeometryCollection)),
          borders: unwrapObject(mesh(topo, countries, (a, b) => a !== b)),
        };
      });
    p.catch(() => basemaps.delete(url)); // a failed fetch is tried again by the next map
    basemaps.set(url, p);
  }
  return p;
}

/** GeoJSON layer options with the renderer its paths are drawn by: Leaflet hands its options to
 * each path it makes (so `renderer` works), but its type definitions leave it out. */
type GeoJsonOptions = L.GeoJSONOptions & { renderer: L.Renderer; pane?: string };
const geoJson = (data: GeoJSON.GeoJsonObject | undefined, options: GeoJsonOptions) => L.geoJSON(data, options);

/** A CSS variable of the map's element, read when drawing (the theme may have changed). */
const cssVar = (el: Element, name: string, fallback: string) =>
  getComputedStyle(el).getPropertyValue(name).trim() || fallback;

function textElement(tag: string, text: string, className?: string): HTMLElement {
  const el = document.createElement(tag);
  el.textContent = text; // never innerHTML: values are the user's data
  if (className) el.className = className;
  return el;
}

function popupFor(item: Item): HTMLElement {
  const box = document.createElement("div");
  box.className = "map-popup";
  box.append(textElement("div", `${item.label} · ${geometrySummary(item.geometry)}`, "map-popup-title"));
  const list = document.createElement("dl");
  for (const [name, type, value] of item.details?.() ?? []) {
    const dt = textElement("dt", name);
    dt.title = type;
    const shown = value === null ? "NULL" : value.length > popupValueCap ? `${value.slice(0, popupValueCap)}…` : value;
    list.append(dt, textElement("dd", shown, value === null ? "grid-null" : undefined));
  }
  if (list.childElementCount) box.append(list);
  return box;
}

interface MapProps {
  items: Item[];
  crs: Crs;
  /** Values that are NULL, empty or not WKB: said in the legend, not drawn. */
  skipped?: { nulls: number; empty: number; unreadable: number; overCap: number };
  compact?: boolean;
  testId?: string;
}

function GeometryMap({ items, crs, skipped, compact = false, testId }: MapProps) {
  const host = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | undefined>();
  const extent = useMemo(() => extentOf(items.map((i) => i.geometry)), [items]);
  const { space, note } = useMemo(() => mapSpace(crs, extent), [crs, extent]);

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const plane = space === "plane";
    const map = L.map(el, {
      crs: plane ? L.CRS.Simple : L.CRS.EPSG3857,
      attributionControl: false,
      zoomControl: true,
      zoomSnap: 0.25,
      zoomDelta: 0.5,
      wheelPxPerZoomLevel: 90,
      minZoom: plane ? -40 : 1,
      maxZoom: plane ? 40 : 19,
      worldCopyJump: false,
    });
    const toLatLng =
      space === "webmercator"
        ? (x: number, y: number) => L.Projection.SphericalMercator.unproject(L.point(x, y))
        : (x: number, y: number) => L.latLng(y, x);

    // The basemap, under the data, in a pane of its own that takes no pointer events.
    let disposed = false;
    if (!plane) {
      const pane = map.createPane("basemap");
      pane.style.zIndex = "250";
      pane.style.pointerEvents = "none";
      const svg = L.svg({ pane: "basemap", padding: 0.5 });
      let shown: { url: string; layer: L.Layer } | undefined;
      const want = () => (map.getZoom() >= detailZoom ? countries50 : countries110);
      const show = () => {
        const url = want();
        if (shown?.url === url) return;
        loadBasemap(url)
          .then((b) => {
            if (disposed || want() !== url) return;
            const layer = L.layerGroup([
              geoJson(b.land, { pane: "basemap", renderer: svg, interactive: false, style: { className: "map-land" } }),
              geoJson(b.borders, {
                pane: "basemap",
                renderer: svg,
                interactive: false,
                style: { className: "map-border" },
              }),
            ]).addTo(map);
            if (shown) map.removeLayer(shown.layer);
            shown = { url, layer };
          })
          .catch((err: unknown) => {
            if (!disposed) setError(`The basemap didn't load (${(err as Error).message}); the data is still drawn.`);
          });
      };
      map.on("zoomend", show);
      map.whenReady(show);
      L.control.scale({ position: "bottomleft" }).addTo(map);
      L.control
        .attribution({ prefix: false, position: "bottomright" })
        .addAttribution("Basemap: Natural Earth")
        .addTo(map);
    }

    // The data.
    const renderer = L.canvas({ padding: 0.5, tolerance: 5 });
    const style = (): L.PathOptions => {
      const accent = cssVar(el, "--map-data", "#f15a24");
      return { color: accent, weight: 2, opacity: 0.9, fillColor: accent, fillOpacity: 0.25 };
    };
    const hover = (): L.PathOptions => ({ color: cssVar(el, "--map-hover", "#31a8db"), weight: 3, fillOpacity: 0.45 });
    const data = geoJson(undefined, {
      renderer,
      coordsToLatLng: (c: number[]) => toLatLng(c[0]!, c[1]!),
      pointToLayer: (_f, latlng) => L.circleMarker(latlng, { renderer, radius: 5, fillOpacity: 0.75 }),
      style,
      onEachFeature: (f, layer) => {
        const item = items[(f.properties as { i: number }).i]!;
        layer.bindTooltip(() => textElement("span", `${item.label} · ${geometrySummary(item.geometry)}`), {
          sticky: true,
          direction: "top",
          offset: [0, -6],
        });
        layer.bindPopup(() => popupFor(item), { maxWidth: 420, autoPanPadding: [24, 24] });
        layer.on("mouseover", () => (layer as L.Path).setStyle(hover()));
        layer.on("mouseout", () => data.resetStyle(layer));
      },
    });
    items.forEach((item, i) => {
      const geometry = toGeoJson(item.geometry);
      if (geometry) data.addData({ type: "Feature", geometry, properties: { i } } as GeoJSON.Feature);
    });
    data.addTo(map);

    // A theme change restyles the data (the basemap follows the CSS on its own).
    const restyle = () => data.setStyle(style);
    const themeWatch = new MutationObserver(restyle);
    themeWatch.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const scheme = window.matchMedia?.("(prefers-color-scheme: dark)");
    scheme?.addEventListener("change", restyle);

    // Fit the data: one point (or a vertical/horizontal line) gets a sensible zoom.
    const e = extent;
    if (e.minX > e.maxX) map.setView([20, 0], plane ? 0 : 1.5);
    else {
      const sw = toLatLng(e.minX, e.minY);
      const ne = toLatLng(e.maxX, e.maxY);
      if (e.minX === e.maxX && e.minY === e.maxY) map.setView(sw, plane ? 0 : compact ? 5 : 9);
      // A single value (the inspector) gets room around it, so the map shows where it is.
      else
        map.fitBounds(L.latLngBounds(sw, ne).pad(compact ? 1.5 : 0.05), {
          padding: [28, 28],
          maxZoom: plane ? 40 : 15,
        });
    }

    // The map's box changes with the page (Expand, the editor's splitter, a dialog opening).
    const resize = new ResizeObserver(() => map.invalidateSize());
    resize.observe(el);
    return () => {
      disposed = true;
      resize.disconnect();
      themeWatch.disconnect();
      scheme?.removeEventListener("change", restyle);
      map.remove();
    };
  }, [items, space, extent, compact]);

  const counts = skipped
    ? [
        skipped.overCap &&
          `${skipped.overCap.toLocaleString()} more not drawn (the map draws at most ${maxFeatures.toLocaleString()})`,
        skipped.nulls && `${skipped.nulls.toLocaleString()} NULL`,
        skipped.empty && `${skipped.empty.toLocaleString()} empty`,
        skipped.unreadable && `${skipped.unreadable.toLocaleString()} not readable as WKB`,
      ].filter(Boolean)
    : [];
  return (
    <div className={`geometry-map-wrap${compact ? " geometry-map-compact" : ""}`} data-testid={testId}>
      <div ref={host} className="geometry-map" data-space={space} />
      <div className="geometry-map-legend" data-testid="map-legend">
        <span>
          {items.length.toLocaleString()} {items.length === 1 ? "geometry" : "geometries"} · {note}
        </span>
        {counts.length > 0 && <span> · {counts.join(" · ")}</span>}
        {error && <span className="geometry-map-error"> · {error}</span>}
      </div>
    </div>
  );
}

/** A geometry column's values, decoded when first needed (like RowTexts for the grid's text). */
class RowGeometries {
  readonly #vector: { get(row: number): unknown };
  readonly #cache = new Map<number, Geometry | "null" | "unreadable" | "empty">();
  constructor(vector: { get(row: number): unknown }) {
    this.#vector = vector;
  }
  get(row: number): Geometry | "null" | "unreadable" | "empty" {
    let d = this.#cache.get(row);
    if (d === undefined) {
      const v = this.#vector.get(row);
      if (v === null || v === undefined) d = "null";
      else {
        try {
          const g = parseWkb(v as Uint8Array);
          d = toGeoJson(g) === null ? "empty" : g;
        } catch {
          d = "unreadable";
        }
      }
      this.#cache.set(row, d);
    }
    return d;
  }
}

/** The results' Map view: the rows the grid shows, in its order, drawn from one geometry column. */
export function ResultsMap({ table, shown, column }: { table: Table; shown: readonly number[]; column: number }) {
  const field = table.schema.fields[column]!;
  const crs = useMemo(() => geometryCrs(field), [field]);
  const formats = useMemo(() => columnText(table.schema), [table]);
  const labels = useMemo(() => table.schema.fields.map((f) => typeLabel(f)), [table]);
  // Each row's geometry, decoded once per result (a new filter or sort reuses them).
  const decoded = useMemo(() => new RowGeometries(table.getChildAt(column)!), [table, column]);
  const { items, skipped } = useMemo(() => {
    const vectors = table.schema.fields.map((_, c) => table.getChildAt(c)!);
    const out: Item[] = [];
    const skip = { nulls: 0, empty: 0, unreadable: 0, overCap: 0 };
    shown.forEach((r, position) => {
      const g = decoded.get(r);
      if (g === "null") skip.nulls++;
      else if (g === "unreadable") skip.unreadable++;
      else if (g === "empty") skip.empty++;
      else if (out.length >= maxFeatures) skip.overCap++;
      else
        out.push({
          geometry: g,
          label: `Row ${(position + 1).toLocaleString()}`,
          details: () =>
            table.schema.fields.map((f, c): [string, string, string | null] => [
              f.name,
              labels[c]!,
              formats[c]!(vectors[c]!.get(r)),
            ]),
        });
    });
    return { items: out, skipped: skip };
  }, [table, shown, decoded, formats, labels]);
  return <GeometryMap items={items} crs={crs} skipped={skipped} testId="results-map" />;
}

/** One geometry, for the cell inspector. */
export function GeometryPreview({ geometry, crs }: { geometry: Geometry; crs: Crs }) {
  const items = useMemo(() => [{ geometry, label: "This value" }], [geometry]);
  if (toGeoJson(geometry) === null) return null;
  return <GeometryMap items={items} crs={crs} compact testId="inspector-map" />;
}
