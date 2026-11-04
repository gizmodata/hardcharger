// Nested schemas (DuckDB 2.0: CREATE SCHEMA a; CREATE SCHEMA a.b) in the schema browser: a
// tree, each schema under its parent, a top-level schema whose name holds a dot ("x.y") a
// root of its own, and names inserted with the full path, quoted where SQL needs it. The
// server lists a nested schema by its path (ui/src/lib/schemaPath.ts); a server that lists
// only leaf names can't be told apart from one with two top-level schemas, so on one the test
// is skipped, with the reason (it passes once the metadata contract is in the build).
import { connect } from "@gizmodata/chargewire";

import { editorFocused, editorText, expect, gridRows, runDone, setSql, signIn, test } from "./fixtures.ts";

test("nested schemas show as a tree, a dotted name as its own root, and names insert their full path", async ({
  page,
  harness,
}) => {
  const admin = await connect({ url: harness.serverUrl, ...harness.admin });
  try {
    for (const sql of [
      "CREATE SCHEMA IF NOT EXISTS nest",
      "CREATE SCHEMA IF NOT EXISTS nest.inner",
      'CREATE SCHEMA IF NOT EXISTS "x.y"',
      "CREATE OR REPLACE TABLE nest.inner.deep AS SELECT 1 AS id",
      "CREATE OR REPLACE TABLE nest.top AS SELECT 2 AS id",
      'CREATE OR REPLACE TABLE "x.y".dotted AS SELECT 3 AS id',
    ]) {
      await admin.execute(sql);
    }
    const listed = await admin.metadata.dbSchemas({ catalog: harness.catalog });
    const names = listed
      .toArray()
      .map((r) => String((r as { toJSON: () => Record<string, unknown> }).toJSON()["db_schema_name"]));
    test.skip(
      !names.includes("nest.inner") || !names.includes('"x.y"'),
      `the server lists the schemas as ${JSON.stringify(names.filter((n) => /inner|x\.y/.test(n)))}, not by their ` +
        'path (nest.inner, "x.y"): enable when the nested-schema metadata contract is in this build',
    );
  } finally {
    await admin.close();
  }
  await signIn(page, harness.admin);
  await editorFocused(page);
  const catalog = page
    .getByRole("tree", { name: "Servers" })
    .getByRole("treeitem", { name: harness.catalog, exact: true });
  const nest = catalog.getByRole("treeitem", { name: "nest", exact: true });
  await nest.getByRole("button", { name: "nest", exact: true }).click();
  await expect(nest).toHaveAttribute("data-schema-path", JSON.stringify(["nest"]));
  const inner = nest.getByRole("treeitem", { name: "inner", exact: true });
  await expect(inner).toHaveAttribute("data-schema-path", JSON.stringify(["nest", "inner"]));
  await expect(catalog.getByRole("treeitem", { name: "inner", exact: true })).toHaveCount(1); // not a second root
  // nest's own tables sit beside its child schema.
  await expect(nest.getByRole("treeitem", { name: "top", exact: true })).toBeVisible();
  await inner.getByRole("button", { name: "inner", exact: true }).click();
  const deep = inner.getByRole("treeitem", { name: "deep", exact: true });
  await expect(deep).toBeVisible();
  // The dotted top-level schema is a root of its own, with the dot in its name.
  const dotted = catalog.getByRole("treeitem", { name: "x.y", exact: true });
  await expect(dotted).toHaveAttribute("data-schema-path", JSON.stringify(["x.y"]));
  await expect(catalog.getByRole("treeitem", { name: "x", exact: true })).toHaveCount(0);
  await dotted.getByRole("button", { name: "x.y", exact: true }).click();
  const dottedTable = dotted.getByRole("treeitem", { name: "dotted", exact: true });
  await expect(dottedTable).toBeVisible();

  // Double-click inserts the full path, each part quoted as SQL needs.
  await setSql(page, "SELECT * FROM ");
  await deep.getByRole("button", { name: "deep", exact: true }).dblclick();
  await expect.poll(() => editorText(page)).toBe(`SELECT * FROM ${harness.catalog}.nest."inner".deep`);
  await setSql(page, "SELECT * FROM ");
  await dottedTable.getByRole("button", { name: "dotted", exact: true }).dblclick();
  await expect.poll(() => editorText(page)).toBe(`SELECT * FROM ${harness.catalog}."x.y".dotted`);
  // And they run.
  await runDone(
    page,
    `SELECT id FROM ${harness.catalog}.nest.inner.deep UNION ALL SELECT id FROM ${harness.catalog}."x.y".dotted ORDER BY 1`,
  );
  expect((await gridRows(page)).slice(1)).toEqual([["1"], ["3"]]);
  // The search finds the nested table under its path.
  await page.getByLabel("Search tables and views").fill("deep");
  await expect(page.getByTestId("search-results").getByRole("button", { name: /deep/ })).toContainText("nest.inner");
});
