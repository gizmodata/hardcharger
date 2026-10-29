import { ChargewireError } from "@gizmodata/chargewire";
import { describe, expect, it } from "vitest";

import { signInLost } from "../../src/session";

describe("signInLost", () => {
  it("says why the session ended, from the problem's reason (Chargewire §5.6)", () => {
    const ended = (reason?: string) =>
      new ChargewireError({ code: "session_ended", status: 401, ...(reason ? { reason } : {}) });
    expect(signInLost(ended("killed"))).toBe("An administrator ended your session. Sign in again.");
    expect(signInLost(ended("credentials_changed"))).toBe("Your password or credentials changed. Sign in again.");
    expect(signInLost(ended("principal_dropped"))).toBe("Your user no longer exists on this server.");
    expect(signInLost(ended("token_expired"))).toBe("Your sign-in has expired. Sign in again.");
    expect(signInLost(ended())).toBe("Your sign-in has expired. Sign in again.");
    expect(signInLost(new ChargewireError({ code: "invalid_token", status: 401 }))).toBe(
      "Your sign-in has expired. Sign in again.",
    );
    expect(signInLost(ended("token_revoked"))).toBe("Your sign-in was revoked. Sign in again.");
    // An unknown reason is a generic one (Chargewire §12).
    expect(signInLost(ended("some_later_reason"))).toBe("Your sign-in has expired. Sign in again.");
  });

  it("says why a token stopped working, from invalid_token's reason (Chargewire §4.2)", () => {
    const refused = (reason: string) => new ChargewireError({ code: "invalid_token", status: 401, reason });
    expect(signInLost(refused("token_revoked"))).toBe("Your sign-in was revoked. Sign in again.");
    expect(signInLost(refused("credentials_changed"))).toBe("Your password or credentials changed. Sign in again.");
    expect(signInLost(refused("principal_dropped"))).toBe("Your user no longer exists on this server.");
    expect(signInLost(refused("token_expired"))).toBe("Your sign-in has expired. Sign in again.");
  });

  it("leaves every other error alone", () => {
    expect(signInLost(new ChargewireError({ code: "sql_error", status: 400 }))).toBeUndefined();
    expect(signInLost(new Error("x"))).toBeUndefined();
  });
});
