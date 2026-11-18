// Only this computer: the local server's administrator credentials on the dashboard (#56).
// The password is generated once and kept in the OS credential store; the controller never
// holds it until a person clicks Reveal or Copy, and forgets it on Hide or when the page changes.
import { invoke } from "@tauri-apps/api/core";
import { copyText } from "./connectionPanel";

export interface LocalAdminStatus { username: string; error: string | null }
interface Status { running: boolean; localAdmin?: LocalAdminStatus | null }

let revealed: string | null = null;
let note = "";
let confirming = false;
let working = false;
const esc = (value: string) => value.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
const masked = "•".repeat(16);

/** Forgets a revealed password (Hide, another page, a new password). */
export function forgetRevealedPassword(): void { revealed = null; note = ""; }

/** The Generate new password confirmation is open (the dashboard doesn't re-render under it). */
export function confirmingNewPassword(): boolean { return confirming; }

export function localAdminCard(status: Status | null, disabled: boolean): string {
  const admin = status?.localAdmin;
  if (!admin) return "";
  const off = disabled || working ? "disabled" : "";
  return `<section class="card detail-card local-admin" aria-label="Administrator credentials">
    <div class="section-heading"><div><h3>Administrator credentials</h3><p>Other programs on this computer (the CLI, Python, JDBC, DBeaver, a browser) sign in to the local server with these. Any program running as you can reach 127.0.0.1 and use them.</p></div>${admin.error ? "" : `<span class="pill">Saved in ${navigator.userAgent.includes("Windows") ? "Credential Manager" : "the Keychain"}</span>`}</div>
    <dl class="credentials">
      <div><dt>User name</dt><dd><code data-testid="local-admin-username">${esc(admin.username)}</code></dd></div>
      <div><dt>Password</dt><dd><code data-testid="local-admin-password" aria-label="${revealed ? "Password" : "Password, hidden"}">${revealed ? esc(revealed) : masked}</code></dd></div>
    </dl>
    <div class="control-actions">
      <button class="button" data-credential="reveal" ${off}>${revealed ? "Hide" : "Reveal"}</button>
      <button class="button" data-credential="copy" ${off}>Copy password</button>
      <button class="button control-restart" data-credential="regenerate" ${off}>Generate new password</button>
    </div>
    <p id="credential-status" class="muted" role="status" aria-live="polite">${esc(note)}</p>
    ${admin.error ? `<p class="network-warning" data-testid="local-admin-error">${esc(admin.error)}</p>` : ""}
  </section>`;
}

export function localAdminDialog(status: Status | null): string {
  if (!confirming) return "";
  const running = status?.running ?? false;
  return `<dialog id="local-password-confirmation" class="trust-dialog" aria-labelledby="local-password-title" aria-describedby="local-password-description"><h2 id="local-password-title">Generate a new administrator password?</h2><p id="local-password-description">Programs that sign in with the current password (the CLI, Python, JDBC, DBeaver, a browser) will need the new one; the current one stops working.</p><p>${running ? "The server restarts now to use it. This will close the SQL workspace and all query-detail windows. Active queries and uploads may be interrupted, client sessions will disconnect, and uncommitted transactions will be rolled back." : "The server uses it the next time it starts."}</p><div class="trust-actions"><button id="cancel-local-password" class="button" autofocus>Cancel</button><button id="confirm-local-password" class="button control-restart">Generate new password</button></div></dialog>`;
}

/**
 * Wires the card's buttons and the confirmation. `done` receives the new status after Generate
 * new password; `fail` shows an error.
 */
export function bindLocalAdmin(render: () => void, done: (status: unknown) => void, fail: (error: unknown) => void): void {
  const password = () => invoke<string>("local_admin_password");
  document.querySelector<HTMLButtonElement>('[data-credential="reveal"]')?.addEventListener("click", () => {
    if (revealed) { forgetRevealedPassword(); render(); return; }
    void password().then(p => { revealed = p; note = ""; render(); }).catch(fail);
  });
  document.querySelector<HTMLButtonElement>('[data-credential="copy"]')?.addEventListener("click", () => {
    void copySecret(revealed ?? password).then(ok => {
      note = ok ? "Password copied to the clipboard." : "Reveal the password and copy it with your keyboard.";
      const status = document.querySelector("#credential-status");
      if (status) status.textContent = note;
    }).catch(fail);
  });
  document.querySelector<HTMLButtonElement>('[data-credential="regenerate"]')?.addEventListener("click", () => {
    confirming = true; render();
  });
  const dialog = document.querySelector<HTMLDialogElement>("#local-password-confirmation");
  if (!dialog) return;
  dialog.showModal();
  const cancel = () => {
    confirming = false; render();
    document.querySelector<HTMLButtonElement>('[data-credential="regenerate"]')?.focus();
  };
  dialog.addEventListener("cancel", e => { e.preventDefault(); cancel(); });
  document.querySelector("#cancel-local-password")?.addEventListener("click", cancel);
  document.querySelector("#confirm-local-password")?.addEventListener("click", () => {
    confirming = false; working = true; forgetRevealedPassword(); render();
    void invoke("regenerate_local_password", { confirmed: true })
      .then(status => { note = "A new password was generated. Reveal or copy it for your clients."; done(status); })
      .catch(fail)
      .finally(() => { working = false; render(); });
  });
}

/**
 * Copies the password through the controller's clipboard path. A password not yet in the page
 * is fetched within the click: WebKit keeps the click's permission for a ClipboardItem whose
 * text is still on its way, and the plain path (a synchronous copy, then the Clipboard API) is
 * the fallback.
 */
async function copySecret(source: string | (() => Promise<string>)): Promise<boolean> {
  if (typeof source === "string") return copyText(source);
  const pending = source();
  if (typeof ClipboardItem !== "undefined" && navigator.clipboard?.write) {
    try {
      await navigator.clipboard.write([new ClipboardItem({ "text/plain": pending.then(text => new Blob([text], { type: "text/plain" })) })]);
      return true;
    } catch { /* the plain path below */ }
  }
  return copyText(await pending);
}
