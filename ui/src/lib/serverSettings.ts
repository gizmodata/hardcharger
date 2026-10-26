import { quoteLiteral } from "./sql";

export interface ServerSetting {
  name: string;
  value: string;
  session_value: string | null;
  global_value: string | null;
  scope: "SESSION" | "GLOBAL" | "SESSION_OR_GLOBAL" | "STARTUP";
  input_type: string;
  settable: boolean;
  cli_flag: string | null;
  default_value: string;
  description: string;
}
export type SettingScope = "SESSION" | "GLOBAL";
export function editableAt(setting: ServerSetting, scope: SettingScope): boolean {
  return setting.settable && (setting.scope === scope || setting.scope === "SESSION_OR_GLOBAL");
}
export function changeSettingSql(
  setting: ServerSetting,
  scope: SettingScope,
  sessionId: string,
  value: string,
  reset = false,
): string {
  if (!editableAt(setting, scope)) throw new Error("This setting cannot be changed at that scope");
  if (!/^hardcharger\.[a-z_][a-z0-9_]*$/.test(setting.name)) throw new Error("Invalid setting name");
  if (scope === "GLOBAL")
    return reset ? `RESET GLOBAL ${setting.name}` : `SET GLOBAL ${setting.name} = ${quoteLiteral(value)}`;
  return reset
    ? `SELECT hardcharger_reset_session_setting(${quoteLiteral(sessionId)}, ${quoteLiteral(setting.name)})`
    : `SELECT hardcharger_set_session_setting(${quoteLiteral(sessionId)}, ${quoteLiteral(setting.name)}, ${quoteLiteral(value)})`;
}
export function sessionSettingValue(setting: ServerSetting, settings: readonly ServerSetting[]): string {
  // An admin without an explicit bypass override inherits the startup admin policy.
  if (setting.name === "hardcharger.bypass_queue" && setting.session_value === null)
    return settings.find((s) => s.name === "hardcharger.admin_bypass_queue_default")?.value ?? setting.value;
  return setting.value;
}
export function settingChoices(setting: ServerSetting): string[] | undefined {
  if (setting.input_type === "BOOLEAN") return ["true", "false"];
  if (setting.name === "hardcharger.capture_query_profile") return ["off", "standard", "detailed"];
  if (setting.name === "hardcharger.query_log_level") return ["DEBUG", "INFO", "WARNING", "ERROR"];
  return undefined;
}
