// What the Hardcharger desktop app offers the SQL workspace it opens: remembered passwords for
// saved connections, kept in the operating system's keychain (macOS Keychain, Windows
// Credential Manager, the Secret Service on Linux) by the app, never in browser storage.
//
// The app grants its workspace window, on the exact origin it opened, the three commands of its
// `remote-passwords` plugin and the two of `remote-servers` (removing or renaming a server in
// Settings > Remote servers, so the workspace and the app agree) and nothing else; it marks the window with
// sessionStorage["hardcharger-ui:desktop-mode"]. In a plain browser there is none of this,
// and the dialog's Remember password stays off (localStorage would keep a password readable
// by any script on the origin, in clear text, for good).

interface TauriInternals {
  invoke: (command: string, args?: Record<string, unknown>) => Promise<unknown>;
}

function internals(): TauriInternals | undefined {
  try {
    if (!window.sessionStorage.getItem("hardcharger-ui:desktop-mode")) return undefined;
  } catch {
    return undefined;
  }
  const tauri = (window as unknown as { __TAURI_INTERNALS__?: Partial<TauriInternals> }).__TAURI_INTERNALS__;
  return typeof tauri?.invoke === "function" ? (tauri as TauriInternals) : undefined;
}

/** Whether passwords can be remembered here (the desktop app's workspace). */
export function canRememberPasswords(): boolean {
  return internals() !== undefined;
}

/** The password remembered for saved connection `id`, if any. */
export async function rememberedPassword(id: string): Promise<string | undefined> {
  const tauri = internals();
  if (!tauri) return undefined;
  const value = await tauri.invoke("plugin:remote-passwords|get", { id });
  return typeof value === "string" && value ? value : undefined;
}

/** Remembers `password` for saved connection `id` in the keychain. */
export async function rememberPassword(id: string, password: string): Promise<void> {
  const tauri = internals();
  if (!tauri) throw new Error("Passwords can be remembered in the Hardcharger desktop app only.");
  await tauri.invoke("plugin:remote-passwords|set", { id, password });
}

/** Forgets the password remembered for saved connection `id` (nothing to do where none can be). */
export async function forgetRememberedPassword(id: string): Promise<void> {
  const tauri = internals();
  if (!tauri) return;
  await tauri.invoke("plugin:remote-passwords|delete", { id });
}

/** The address of a saved connection, which the app matches its own entries by (and by id). */
interface ServerRef {
  id: string;
  host: string;
  port: number;
  prefix: string;
}

const ref = (s: ServerRef) => ({ id: s.id, host: s.host, port: s.port, prefix: s.prefix });

/** Removes the server from the desktop app's Settings > Remote servers (nothing to do elsewhere). */
export async function removeDesktopServer(server: ServerRef): Promise<void> {
  const tauri = internals();
  if (!tauri) return;
  await tauri.invoke("plugin:remote-servers|remove", { server: ref(server) });
}

/** Renames the server in the desktop app's Settings > Remote servers (nothing to do elsewhere). */
export async function renameDesktopServer(server: ServerRef, name: string): Promise<void> {
  const tauri = internals();
  if (!tauri) return;
  await tauri.invoke("plugin:remote-servers|rename", { server: ref(server), name });
}
