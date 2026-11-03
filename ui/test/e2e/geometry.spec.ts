import { readFileSync } from "node:fs";
import { tableFromIPC } from "apache-arrow";
import { readParquet } from "parquet-wasm";
import { type Page } from "@playwright/test";
import { activeTab, expect, gridRows, runDone, signIn, test } from "./fixtures.ts";

// GEOMETRY results: WKT in the grid (as DuckDB's ST_AsText writes it), a Map view of the rows
// shown, and a map of one value in the cell inspector. Everything the maps load is the UI's own
// (the fixtures fail a test on any CSP violation); this also checks nothing leaves the origin.

const cities = `SELECT * FROM (VALUES
    (1, 'Charlotte', 'POINT (-80.8431 35.2271)'::GEOMETRY),
    (2, 'Paris', 'POINT (2.3522 48.8566)'::GEOMETRY),
    (3, 'Nowhere', NULL::GEOMETRY),
    (4, 'Triangle', 'POLYGON ((-100 30, -90 30, -95 40, -100 30))'::GEOMETRY)
  ) t(id, name, geom) ORDER BY id`;

/** Every request the page makes to an origin other than the UI's. */
function foreignRequests(page: Page, origin: string): string[] {
  const out: string[] = [];
  page.on("request", (r) => {
    if (!r.url().startsWith(origin) && !r.url().startsWith("data:")) out.push(r.url());
  });
  return out;
}

test("GEOMETRY shows as WKT, typed in the header, in the grid, the filter and the downloads", async ({
  page,
  harness,
}) => {
  await signIn(page, harness.admin);
  await runDone(page, cities);
  const rows = await gridRows(page);
  expect(rows[1]).toEqual(["1", "Charlotte", "POINT (-80.8431 35.2271)"]);
  expect(rows[3]).toEqual(["3", "Nowhere", "NULL"]);
  expect(rows[4]).toEqual(["4", "Triangle", "POLYGON ((-100 30, -90 30, -95 40, -100 30))"]);
  await expect(activeTab(page).locator(".grid-head-type").nth(2)).toHaveText("GEOMETRY");

  await activeTab(page).getByTestId("results-filter").fill("POINT (2.35");
  await expect(activeTab(page).getByTestId("results-count")).toHaveText("1 of 4 rows match");
  await activeTab(page).getByTestId("results-filter").fill("");

  // Text downloads carry WKT; typed ones keep the geometry (geoarrow.wkb, its bytes).
  for (const label of ["CSV (comma-separated)", "Parquet"]) {
    await activeTab(page).getByTestId("export-button").click();
    const download = page.waitForEvent("download");
    await page.getByRole("menuitem", { name: label, exact: true }).click();
    const bytes = readFileSync(await (await download).path());
    if (label === "Parquet") {
      const table = tableFromIPC(readParquet(new Uint8Array(bytes)).intoIPCStream());
      expect(table.schema.fields[2]!.metadata.get("ARROW:extension:name")).toBe("geoarrow.wkb");
      expect(table.getChildAt(2)!.get(0)).toBeInstanceOf(Uint8Array);
    } else {
      expect(bytes.toString()).toContain("Charlotte,POINT (-80.8431 35.2271)");
    }
  }
});

test("the Map view draws the rows shown, on the UI's own basemap, and a click shows the row", async ({
  page,
  harness,
}) => {
  const foreign = foreignRequests(page, new URL(harness.url).origin);
  await signIn(page, harness.admin);
  await runDone(page, cities);
  const tab = activeTab(page);
  await tab.getByTestId("results-view-map").click();
  const map = tab.getByTestId("results-map");
  await expect(map).toBeVisible();
  await expect(tab.getByTestId("results-grid")).toBeHidden();
  await expect(map.getByTestId("map-legend")).toHaveText(
    "3 geometries · No CRS on the column: drawn as longitude, latitude · 1 NULL",
  );
  // The basemap (land and borders, SVG) and the data (a canvas).
  await expect(map.locator("path.map-land").first()).toBeAttached();
  await expect(map.locator("path.map-border").first()).toBeAttached();
  await expect(map.locator("canvas")).toBeAttached();
  await expect(map.locator(".leaflet-control-attribution")).toHaveText("Basemap: Natural Earth");

  // The grid's filter decides what the map draws: one point, centred, so a click there hits it.
  await tab.getByTestId("results-filter").fill("Paris");
  await expect(map.getByTestId("map-legend")).toHaveText(
    "1 geometry · No CRS on the column: drawn as longitude, latitude",
  );
  const box = (await map.locator(".geometry-map").boundingBox())!;
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  const popup = map.locator(".map-popup");
  await expect(popup.locator(".map-popup-title")).toHaveText("Row 1 · POINT");
  await expect(popup.locator("dd")).toHaveText(["2", "Paris", "POINT (2.3522 48.8566)"]);

  // Back to the grid, which kept its filter.
  await tab.getByTestId("results-view-grid").click();
  await expect(tab.getByTestId("results-grid")).toBeVisible();
  expect((await gridRows(page))[1]).toEqual(["2", "Paris", "POINT (2.3522 48.8566)"]);
  expect(foreign).toEqual([]);
});

test("the inspector maps a geometry and gives its GeoJSON; other CRSs draw without a basemap", async ({
  page,
  harness,
}) => {
  await signIn(page, harness.admin);
  await runDone(page, cities);
  const grid = activeTab(page).getByTestId("results-grid");
  await grid.locator('[role="gridcell"]').filter({ hasText: "POLYGON" }).dblclick();
  const inspector = page.getByTestId("cell-inspector");
  await expect(inspector.getByTestId("inspector-value")).toHaveText("POLYGON ((-100 30, -90 30, -95 40, -100 30))");
  await expect(inspector.getByTestId("inspector-map").getByTestId("map-legend")).toHaveText(
    "1 geometry · No CRS on the column: drawn as longitude, latitude",
  );
  await inspector.getByTestId("inspector-geojson").locator("summary").click();
  expect(JSON.parse(await inspector.getByTestId("inspector-geojson").locator("pre").innerText())).toEqual({
    type: "Polygon",
    coordinates: [
      [
        [-100, 30],
        [-90, 30],
        [-95, 40],
        [-100, 30],
      ],
    ],
  });
  await page.keyboard.press("Escape");
  await expect(inspector).toBeHidden();

  // Each kind of space. DuckDB core names few CRSs without the spatial extension (OGC:CRS84),
  // so the others are given as PROJJSON, which it takes as is.
  const crs = (type: string, code: number) =>
    JSON.stringify({ type, name: `EPSG ${code}`, id: { authority: "EPSG", code } });
  await runDone(
    page,
    `SELECT 'POINT (514000 3898000)'::GEOMETRY AS plain,
       'POINT (514000 3898000)'::GEOMETRY('${crs("ProjectedCRS", 32617)}') AS utm,
       'POINT (-8999266 4193988)'::GEOMETRY('${crs("ProjectedCRS", 3857)}') AS mercator,
       'POINT (1 2)'::GEOMETRY('OGC:CRS84') AS crs84`,
  );
  const tab = activeTab(page);
  await expect(tab.locator(".grid-head-type")).toHaveText([
    "GEOMETRY",
    "GEOMETRY('EPSG:32617')",
    "GEOMETRY('EPSG:3857')",
    "GEOMETRY('OGC:CRS84')",
  ]);
  await tab.getByTestId("results-view-map").click();
  const map = tab.getByTestId("results-map");
  const legend = map.getByTestId("map-legend");
  const container = map.locator(".leaflet-container");
  await expect(legend).toHaveText("1 geometry · No CRS, and the coordinates aren't degrees: drawn as plain x, y");
  await expect(container).toHaveAttribute("data-space", "plane");
  await expect(map.locator("path.map-land")).toHaveCount(0);
  const column = tab.getByTestId("map-column");
  await column.selectOption({ label: "utm" });
  await expect(legend).toHaveText("1 geometry · EPSG:32617: drawn as plain x, y (no basemap for this CRS)");
  await expect(map.locator("path.map-land")).toHaveCount(0);
  await column.selectOption({ label: "mercator" });
  await expect(legend).toHaveText("1 geometry · EPSG:3857 (Web Mercator), shown on the world map");
  await expect(container).toHaveAttribute("data-space", "webmercator");
  await expect(map.locator("path.map-land").first()).toBeAttached();
  await column.selectOption({ label: "crs84" });
  await expect(legend).toHaveText("1 geometry · OGC:CRS84: longitude, latitude");
  await expect(container).toHaveAttribute("data-space", "lonlat");
});
