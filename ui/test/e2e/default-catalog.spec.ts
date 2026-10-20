// A server whose sessions start in a catalog of its init SQL (default_catalog and
// default_schema): the Catalog and Schema pickers show it from the start, and unqualified
// names resolve there.
import { type IdleServer, startOwnServer } from "./harness.ts";
import { activeTab, editorFocused, expect, gridRows, runDone, signIn, test } from "./fixtures.ts";

let own: IdleServer;

test.beforeAll(async () => {
  // The options as environment variables: a server without them starts, and the test
  // fails on what the pickers show.
  own = await startOwnServer(
    [
      "--init-sql-commands",
      "ATTACH ':memory:' AS lake; CREATE SCHEMA lake.sales; CREATE TABLE lake.sales.orders AS SELECT 42 AS n;",
    ],
    { HARDCHARGER_DEFAULT_CATALOG: "lake", HARDCHARGER_DEFAULT_SCHEMA: "sales" },
  );
});
test.afterAll(async () => {
  await own?.stop();
});

test("the pickers show the server's default catalog and schema", async ({ page }) => {
  await signIn(page, own.admin, own.url);
  await editorFocused(page);
  const tab = activeTab(page);
  await expect(tab.getByLabel("Catalog")).toHaveValue("lake");
  await expect(tab.getByLabel("Schema")).toHaveValue("sales");
  await runDone(page, "SELECT n FROM orders");
  await expect.poll(async () => (await gridRows(page))[1]?.[0]).toBe("42");
});
