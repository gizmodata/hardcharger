import { WarpSqlError } from "@gizmodata/warp-sql";
import { describe, expect, it } from "vitest";

import { signInLost } from "../../src/session";

describe("signInLost", () => {
  it("says why the session ended, from the problem's reason (Warp SQL §5.6)", () => {
    const ended = (reason?: string) =>
      new WarpSqlError({ code: "session_ended", status: 401, ...(reason ? { reason } : {}) });
    expect(signInLost(ended("killed"))).toBe("An administrator ended your session. Sign in again.");
    expect(signInLost(ended("credentials_changed"))).toBe("Your password or credentials changed. Sign in again.");
    expect(signInLost(ended("principal_dropped"))).toBe("Your user no longer exists on this server.");
    expect(signInLost(ended("token_expired"))).toBe("Your sign-in has expired. Sign in again.");
    expect(signInLost(ended())).toBe("Your sign-in has expired. Sign in again.");
    expect(signInLost(new WarpSqlError({ code: "invalid_token", status: 401 }))).toBe(
      "Your sign-in has expired. Sign in again.",
    );
  });

  it("leaves every other error alone", () => {
    expect(signInLost(new WarpSqlError({ code: "sql_error", status: 400 }))).toBeUndefined();
    expect(signInLost(new Error("x"))).toBeUndefined();
  });
});
