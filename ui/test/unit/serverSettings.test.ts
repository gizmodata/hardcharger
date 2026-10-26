import { describe, expect, it } from "vitest";
import { changeSettingSql, editableAt, sessionSettingValue, type ServerSetting } from "../../src/lib/serverSettings";

const setting: ServerSetting = {
  name: "hardcharger.query_timeout",
  value: "21",
  session_value: null,
  global_value: "21",
  scope: "SESSION_OR_GLOBAL",
  input_type: "INTEGER",
  settable: true,
  cli_flag: "--query-timeout-seconds",
  default_value: "0",
  description: "Timeout",
};
describe("runtime configuration changes", () => {
  it("exposes only supported scopes, using server scope metadata", () => {
    expect(editableAt(setting, "SESSION")).toBe(true);
    expect(editableAt(setting, "GLOBAL")).toBe(true);
    expect(editableAt({ ...setting, scope: "GLOBAL" }, "SESSION")).toBe(false);
    expect(editableAt({ ...setting, scope: "SESSION" }, "GLOBAL")).toBe(false);
    expect(editableAt({ ...setting, scope: "STARTUP", settable: false }, "GLOBAL")).toBe(false);
    expect(() => changeSettingSql({ ...setting, scope: "STARTUP", settable: false }, "SESSION", "s_x", "1")).toThrow();
  });
  it("quotes values, targets one session, and resets without inventing a default", () => {
    expect(changeSettingSql(setting, "GLOBAL", "s_x", "42")).toBe("SET GLOBAL hardcharger.query_timeout = '42'");
    expect(changeSettingSql(setting, "SESSION", "s_'x", "a'b")).toBe(
      "SELECT hardcharger_set_session_setting('s_''x', 'hardcharger.query_timeout', 'a''b')",
    );
    expect(changeSettingSql(setting, "GLOBAL", "s_x", "", true)).toBe("RESET GLOBAL hardcharger.query_timeout");
    expect(changeSettingSql(setting, "SESSION", "s_x", "", true)).toBe(
      "SELECT hardcharger_reset_session_setting('s_x', 'hardcharger.query_timeout')",
    );
  });
  it("shows the inherited admin bypass policy accurately", () => {
    const bypass = {
      ...setting,
      name: "hardcharger.bypass_queue",
      value: "false",
      global_value: null,
      scope: "SESSION" as const,
    };
    expect(
      sessionSettingValue(bypass, [{ ...setting, name: "hardcharger.admin_bypass_queue_default", value: "true" }]),
    ).toBe("true");
    expect(sessionSettingValue({ ...bypass, session_value: "false" }, [])).toBe("false");
  });
});
