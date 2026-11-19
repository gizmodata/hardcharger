// A temporary password: the server runs nothing on such a session but the user's own change.
// The UI recognizes that refusal, asks for the current and a new password, sends the one
// statement the server runs (quoting both), and signs in again with the new password, since
// the change ends the session it ran in.
import { type Connection, ChargewireError } from "@gizmodata/chargewire";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChangePasswordPage } from "../../src/components/ChangePasswordPage";
import { changeOwnPasswordSql, passwordExpired, passwordMustChange, sqlString } from "../../src/lib/passwordChange";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const held = new ChargewireError({
  code: "permission_denied",
  status: 403,
  detail:
    "Hardcharger blocked this statement: the password of user carl was set by someone else and must be changed " +
    "before anything else runs: ALTER USER CURRENT_USER WITH PASSWORD '<new>' REPLACE '<current>'",
});

const expired = new ChargewireError({
  code: "permission_denied",
  status: 403,
  detail:
    "Hardcharger blocked this statement: the password of user carl has expired (password_max_age_days) and must " +
    "be changed before anything else runs: ALTER USER CURRENT_USER WITH PASSWORD '<new>' REPLACE '<current>'",
});

describe("recognizing a temporary password", () => {
  it("tells an expired password from one someone else set", () => {
    expect(passwordMustChange(expired)).toBe(true);
    expect(passwordExpired(expired)).toBe(true);
    expect(passwordExpired(held)).toBe(false);
    expect(passwordExpired(new Error("has expired (password_max_age_days)"))).toBe(false);
  });

  it("is the server's hold, and nothing else", () => {
    expect(passwordMustChange(held)).toBe(true);
    expect(
      passwordMustChange(new ChargewireError({ code: "permission_denied", status: 403, detail: "no SELECT" })),
    ).toBe(false);
    expect(passwordMustChange(new Error("network"))).toBe(false);
  });

  it("quotes both passwords as SQL strings", () => {
    expect(sqlString("it's")).toBe("'it''s'");
    expect(changeOwnPasswordSql("n'ew", "o''ld")).toBe(
      "ALTER USER CURRENT_USER WITH PASSWORD 'n''ew' REPLACE 'o''''ld'",
    );
  });
});

function fakeConnection(answer: (sql: string) => Promise<unknown>) {
  const ran: string[] = [];
  const close = vi.fn(async () => {});
  const connection = {
    query: vi.fn(async (sql: string) => {
      ran.push(sql);
      return answer(sql);
    }),
    close,
  } as unknown as Connection;
  return { connection, ran, close };
}

function fill(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

describe("the change-password page", () => {
  it("says an expired password has expired", () => {
    const { connection } = fakeConnection(async () => ({}));
    render(
      <ChangePasswordPage
        auth={{ username: "carl", token: "t1", expiresAt: Date.now() + 60_000 }}
        connection={connection}
        expired
        onChanged={() => {}}
        onSignOut={() => {}}
      />,
    );
    expect(screen.getByTestId("password-must-change").textContent).toBe(
      "The password of carl has expired. Choose a new one before you go on.",
    );
  });

  it("changes the password, then signs in again with the new one", async () => {
    const { connection, ran, close } = fakeConnection(async () => ({}));
    const fetch = vi.fn(async () => Response.json({ access_token: "t2", expires_in: 3600 }));
    vi.stubGlobal("fetch", fetch);
    const onChanged = vi.fn();
    render(
      <ChangePasswordPage
        auth={{ username: "carl", token: "t1", expiresAt: Date.now() + 60_000 }}
        connection={connection}
        onChanged={onChanged}
        onSignOut={() => {}}
      />,
    );
    expect(screen.getByTestId("password-must-change").textContent).toContain("The password of carl was set by");
    fill("Current password", "temp'1");
    fill("New password", "mine");
    fill("New password again", "mine");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    });
    expect(ran).toEqual(["ALTER USER CURRENT_USER WITH PASSWORD 'mine' REPLACE 'temp''1'"]);
    expect(close).toHaveBeenCalled();
    const sent = (fetch.mock.calls[0] as unknown as [string, RequestInit])[1].body;
    const body = JSON.parse(typeof sent === "string" ? sent : "") as object;
    expect(body).toEqual({ grant_type: "password", username: "carl", password: "mine" });
    expect(onChanged).toHaveBeenCalledWith(expect.objectContaining({ username: "carl", token: "t2" }));
  });

  it("says why the server refused, and sends nothing for passwords that don't match", async () => {
    const refused = new ChargewireError({
      code: "permission_denied",
      status: 403,
      detail: "Hardcharger blocked ALTER USER: the current password given with REPLACE is not user carl's password",
    });
    const { connection, ran } = fakeConnection(async () => Promise.reject(refused));
    const onChanged = vi.fn();
    render(
      <ChangePasswordPage
        auth={{ username: "carl", token: "t1", expiresAt: Date.now() + 60_000 }}
        connection={connection}
        onChanged={onChanged}
        onSignOut={() => {}}
      />,
    );
    fill("Current password", "wrong");
    fill("New password", "a");
    fill("New password again", "b");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    });
    expect(screen.getByTestId("password-change-error").textContent).toBe("The new passwords do not match.");
    expect(ran).toEqual([]);
    fill("New password again", "a");
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    });
    expect(screen.getByTestId("password-change-error").textContent).toBe(
      "Hardcharger blocked ALTER USER: the current password given with REPLACE is not user carl's password",
    );
    expect(onChanged).not.toHaveBeenCalled();
  });
});
