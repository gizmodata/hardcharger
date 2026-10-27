import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

// In-app updates (gizmodata/hardcharger#7). The native side checks the signed release feed,
// downloads and verifies the package, and installs it; this module only shows what it found
// and asks before anything stops the server.

export interface UpdatePreferences { automatic: boolean; prereleases: boolean; skipped: string | null }
export interface UpdateState {
  current: string; checking: boolean; checkedAt: number | null;
  available: { version: string; notes: string; date: string | null } | null;
  installing: boolean; error: string | null; preferences: UpdatePreferences;
}
interface Progress { downloaded: number; total: number | null }

let state: UpdateState | null = null;
let dialog: "details" | "consent" | null = null;
let progress: Progress | null = null;
let actionError = "";
const esc = (value: string) => value.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

const offered = () => state?.available && state.available.version !== state.preferences.skipped ? state.available : null;
const releaseUrl = (version: string) => `https://github.com/gizmodata/hardcharger/releases/tag/v${encodeURIComponent(version)}`;
const megabytes = (bytes: number) => `${(bytes / 1048576).toFixed(1)} MB`;

/** The header badge: shown for an available version the user hasn't skipped. */
export function updateBadge(): string {
  const update = offered();
  if (!update) return "";
  return `<button id="update-badge" class="update-badge" aria-haspopup="dialog">${state!.installing ? "Updating…" : `Update available · ${esc(update.version)}`}</button>`;
}

function checkedText(): string {
  if (!state) return "";
  if (state.checking) return "Checking for updates…";
  if (state.error) return `Couldn't check for updates: ${state.error}`;
  if (state.available) return `Hardcharger ${state.available.version} is available.`;
  if (state.checkedAt) return `Hardcharger is up to date. Last checked ${new Date(state.checkedAt * 1000).toLocaleString()}.`;
  return "";
}

/** The About card's section: this app's version, a manual check, and the update settings. */
export function updateAbout(serverVersion: string | undefined): string {
  if (!state) return "";
  const p = state.preferences;
  return `<section class="update-section" aria-label="Updates">
    <p><strong>Hardcharger Desktop ${esc(state.current)}</strong>${serverVersion ? `<br><span class="muted">${esc(serverVersion)}</span>` : ""}</p>
    <div class="update-actions"><button id="check-updates" class="button" ${state.checking || state.installing ? "disabled" : ""}>Check for updates</button>${state.available ? `<button id="show-update" class="button primary">See what's new in ${esc(state.available.version)}</button>` : ""}</div>
    <p id="update-status" class="muted" role="status" aria-live="polite">${esc(checkedText())}</p>
    <label class="update-toggle"><input id="update-automatic" type="checkbox" ${p.automatic ? "checked" : ""}>Check for updates automatically</label>
    <label class="update-toggle"><input id="update-prereleases" type="checkbox" ${p.prereleases ? "checked" : ""}>Include release candidates</label>
  </section>`;
}

/** The release notes dialog and, when the server is running, the confirmation to stop it. */
export function updateDialogs(running: boolean, startOnOpen: boolean): string {
  const update = state?.available;
  if (!dialog || !update) return "";
  if (dialog === "consent") {
    return `<dialog id="update-consent" class="trust-dialog" aria-labelledby="update-consent-title" aria-describedby="update-consent-description"><h2 id="update-consent-title">Restart Hardcharger to update?</h2><p id="update-consent-description">Installing ${esc(update.version)} stops the server first. This will close the SQL workspace and all query-detail windows. Active queries and uploads may be interrupted, client sessions will disconnect, and uncommitted transactions will be rolled back.</p><p>Hardcharger then restarts with the new version${startOnOpen ? " and starts the server again. Clients will need to reconnect." : ". The server stays stopped until you start it, as your settings ask."}</p><div class="trust-actions"><button id="cancel-update-consent" class="button" autofocus>Cancel</button><button id="confirm-update-consent" class="button primary">Stop server and update</button></div></dialog>`;
  }
  const installing = state!.installing;
  const percent = progress?.total ? Math.min(100, Math.round(progress.downloaded / progress.total * 100)) : null;
  const status = installing
    ? `<div class="update-progress" role="status" aria-live="polite"><progress ${percent === null ? "" : `value="${percent}" max="100"`} aria-label="Download progress"></progress><span>${progress ? `Downloading ${megabytes(progress.downloaded)}${progress.total ? ` of ${megabytes(progress.total)}` : ""}` : "Preparing the download…"}</span></div>`
    : "";
  const failure = actionError || (state!.error && !installing ? state!.error : "");
  return `<dialog id="update-details" class="trust-dialog update-dialog" aria-labelledby="update-title"><h2 id="update-title">Hardcharger ${esc(update.version)} is available</h2><p>You have ${esc(state!.current)}.${update.date ? ` Released ${esc(update.date)}.` : ""}</p><div class="release-notes" tabindex="0" aria-label="Release notes">${update.notes.trim() ? esc(update.notes.trim()) : "No release notes."}</div><p><a data-marketing-link href="${releaseUrl(update.version)}" target="_blank" rel="noopener noreferrer">Release on GitHub ↗</a></p>${status}${failure ? `<p class="network-warning" role="alert">${esc(failure)}</p>` : ""}<div class="trust-actions"><button id="skip-update" class="button" ${installing ? "disabled" : ""}>Skip this version</button><button id="close-update" class="button" autofocus ${installing ? "disabled" : ""}>Later</button><button id="install-update" class="button primary" ${installing ? "disabled" : ""}>${running ? "Download and restart" : "Download and install"}</button></div></dialog>`;
}

async function install(confirmed: boolean, render: () => void) {
  actionError = ""; progress = null; dialog = "details";
  if (state) state.installing = true;
  render();
  try { await invoke("update_install", { confirmed }); }
  catch (e) { actionError = String(e); if (state) state.installing = false; render(); }
}

async function savePreferences(preferences: UpdatePreferences, render: () => void) {
  try { state = await invoke<UpdateState>("update_preferences", { preferences }); actionError = ""; }
  catch (e) { actionError = String(e); }
  render();
}

export function bindUpdates(render: () => void, running: () => boolean): void {
  document.querySelector("#update-badge")?.addEventListener("click", () => { dialog = "details"; render(); });
  document.querySelector("#show-update")?.addEventListener("click", () => { dialog = "details"; render(); });
  document.querySelector("#check-updates")?.addEventListener("click", async () => {
    if (state) state.checking = true;
    render();
    try { state = await invoke<UpdateState>("update_check"); } catch (e) { if (state) { state.checking = false; state.error = String(e); } }
    render();
  });
  document.querySelector<HTMLInputElement>("#update-automatic")?.addEventListener("change", e => {
    if (state) void savePreferences({ ...state.preferences, automatic: (e.target as HTMLInputElement).checked }, render);
  });
  document.querySelector<HTMLInputElement>("#update-prereleases")?.addEventListener("change", e => {
    if (state) void savePreferences({ ...state.preferences, prereleases: (e.target as HTMLInputElement).checked }, render);
  });
  const details = document.querySelector<HTMLDialogElement>("#update-details");
  if (details) {
    details.showModal();
    const close = () => { if (state?.installing) return; dialog = null; actionError = ""; render(); };
    details.addEventListener("cancel", e => { e.preventDefault(); close(); });
    document.querySelector("#close-update")?.addEventListener("click", close);
    document.querySelector("#skip-update")?.addEventListener("click", () => {
      if (!state?.available) return;
      dialog = null;
      void savePreferences({ ...state.preferences, skipped: state.available.version }, render);
    });
    document.querySelector("#install-update")?.addEventListener("click", () => {
      // Like Stop and Restart: never stop a running server without asking.
      if (running()) { dialog = "consent"; render(); } else void install(false, render);
    });
  }
  const consent = document.querySelector<HTMLDialogElement>("#update-consent");
  if (consent) {
    consent.showModal();
    const cancel = () => { dialog = "details"; render(); };
    consent.addEventListener("cancel", e => { e.preventDefault(); cancel(); });
    document.querySelector("#cancel-update-consent")?.addEventListener("click", cancel);
    document.querySelector("#confirm-update-consent")?.addEventListener("click", () => void install(true, render));
  }
}

export function startUpdates(render: () => void): void {
  const load = async () => { try { state = await invoke<UpdateState>("update_state"); render(); } catch { /* updates are optional */ } };
  void listen("update-changed", () => void load());
  // Chunks arrive many times a second; redraw only when the shown tenth of a megabyte changes.
  void listen<Progress>("update-progress", e => {
    const shown = (p: Progress | null) => p ? Math.floor(p.downloaded / 104858) : -1;
    const redraw = shown(e.payload) !== shown(progress);
    progress = e.payload;
    if (redraw) render();
  });
  void load();
}
