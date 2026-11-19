import { randomBytes } from "node:crypto";

import { connect } from "@gizmodata/chargewire";

import { expect, expectProblem, gridRows, runSql, test } from "./fixtures.ts";

// A temporary password (one an administrator set WITH ... EXPIRE, or a help desk's reset): the
// server runs nothing on its session but the user's own change. The UI asks for a new password
// at once, changes it, signs in again with it, and goes on as usual.
test("a user signed in with a temporary password chooses a new one first", async ({ page, harness }) => {
  const username = `temp_${randomBytes(3).toString("hex")}`;
  const temporary = `t:${randomBytes(4).toString("hex")}@x`;
  const chosen = `c:${randomBytes(4).toString("hex")}'q`;
  const admin = await connect({ url: harness.serverUrl, ...harness.admin });
  await admin.execute(`CREATE USER ${username} WITH PASSWORD '${temporary}' EXPIRE`);
  await admin.close();

  // The UI's first question (who am I?) is refused until the password changes.
  expectProblem(page, { status: 403, url: /\/chargewire\/v1\/sessions\//, required: false });
  // The change ends the session it ran in; closing it afterwards finds it gone.
  expectProblem(page, { status: 401, url: /\/chargewire\/v1\/sessions\//, required: false });
  expectProblem(page, { status: 404, url: /\/chargewire\/v1\/sessions\//, required: false });
  await page.goto("/ui/");
  await page.getByLabel("User name").fill(username);
  await page.getByLabel("Password").fill(temporary);
  await page.getByRole("button", { name: "Sign in" }).click();

  const form = page.getByRole("form", { name: "Change your password" });
  await expect(form.getByTestId("password-must-change")).toHaveText(
    `The password of ${username} was set by someone else. Choose your own before you go on.`,
  );
  // A wrong current password: the server's refusal is shown, and nothing changes.
  await form.getByLabel("Current password").fill("not-it");
  await form.getByLabel("New password", { exact: true }).fill(chosen);
  await form.getByLabel("New password again").fill(chosen);
  await form.getByRole("button", { name: "Change password" }).click();
  await expect(form.getByTestId("password-change-error")).toContainText(
    "the current password given with REPLACE is not user",
  );
  await form.getByLabel("Current password").fill(temporary);
  await form.getByRole("button", { name: "Change password" }).click();

  // Signed in again with the new password, and the session runs anything the user may.
  await expect(page.getByTestId("header-user")).toContainText(username);
  await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => {});
  await runSql(page, "SELECT 41 + 1 AS answer");
  expect((await gridRows(page))[1]).toEqual(["42"]);

  // The temporary password no longer signs in; the chosen one does, with no hold.
  const check = await connect({ url: harness.serverUrl, username, password: chosen });
  const flag = (await check.query("SELECT password_must_change AS m FROM hardcharger_users()")).get(0)!.toJSON();
  expect(flag["m"]).toBe(false);
  await check.close();
  await expect(connect({ url: harness.serverUrl, username, password: temporary })).rejects.toThrow();
});
