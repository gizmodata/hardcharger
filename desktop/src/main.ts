import { invoke, isTauri } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { stringify } from "yaml";
import { defaultNetwork, validateNetwork, type NetworkSettings } from "./network";
import { parseConfiguration, exportConfiguration, optionChoices, choiceValue, type ServerOption } from "./configuration";
import "./style.css";
import "./palette.css";

interface Settings { port: number; start_on_open: boolean; server_options: Record<string, unknown>; network?: NetworkSettings }
interface Status {
  running: boolean; pid: number | null; port: number; endpoint: string;
  database: string; logFile: string; uptimeSeconds: number; databaseBytes: number;
  version: string; error: string | null; settings: Settings;
}

const native = isTauri();
const designPreview = !native && new URLSearchParams(location.search).get("preview") === "1";
const app = document.querySelector<HTMLDivElement>("#app")!;
let tab = "server";
let busy = false;
let current: Status | null = null;
let error = "";
let login = false;
let schema: ServerOption[] = [];
let settingsNotice = "";
let settingsSearch = "";
let pendingImport: Record<string, unknown> | null = null;
let theme = localStorage.getItem("hardcharger-desktop:theme") ?? "system";
document.documentElement.dataset.theme = theme;

const icons: Record<string, string> = {
  server: '<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M11 6.5h6M11 17.5h6"/>',
  workspace: '<path d="m5 6 5 6-5 6m8 0h6"/>',
  logs: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  settings: '<path d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z"/><path d="m9 3-1 3-3 1-2 3 2 2-1 3 2 3h3l2 3 3-1 1-3 3-1 2-3-2-2 1-3-2-3h-3l-2-2Z"/>',
  arrow: '<path d="M5 12h14m-6-6 6 6-6 6"/>',
  play: '<path d="m8 5 11 7-11 7Z"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
  restart: '<path d="M20 11a8 8 0 1 0-2 7M20 4v7h-7"/>',
  folder: '<path d="M3 7V5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1Z"/>',
  shield: '<path d="m12 3 8 4v5c0 5-8 9-8 9s-8-4-8-9V7Z"/><path d="m8 12 3 3 5-6"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',
};
const icon = (name: string) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] ?? icons.info}</svg>`;
const esc = (text: string | number) => String(text).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
const picture = (name: string, alt: string, className = "") => `<span class="brand-image ${className}"><img class="brand-light" src="/brand/${name}-light.${name === "header" ? "svg" : "webp"}" alt="${alt}"><img class="brand-dark" src="/brand/${name}-dark.${name === "header" ? "svg" : "webp"}" alt="${alt}"></span>`;
const uptime = (n: number) => n < 60 ? `${n}s` : n < 3600 ? `${Math.floor(n / 60)}m ${n % 60}s` : `${Math.floor(n / 3600)}h ${Math.floor(n % 3600 / 60)}m`;
const bytes = (n: number) => n < 1024 * 1024 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;

function button(action: string, label: string, glyph: string, primary = false, disabled = false) {
  return `<button data-action="${action}" class="button ${primary ? "primary" : ""}" ${disabled || busy || !native ? "disabled" : ""}>${icon(glyph)}${label}</button>`;
}

function render() {
  const s = current;
  const running = s?.running ?? false;
  app.innerHTML = `
    <aside class="sidebar">
      <div class="identity">${picture("header", "Hardcharger™")}<span class="edition">DESKTOP PREVIEW</span></div>
      <div class="nav-label">WORKSPACE</div>
      <nav aria-label="Main navigation">
        <button data-tab="server" class="nav-item ${tab === "server" ? "active" : ""}">${icon("server")}Local server<span class="nav-dot ${running ? "running" : ""}"></span></button>
        <button data-action="workspace" class="nav-item" ${!running || !native || busy ? "disabled" : ""}>${icon("workspace")}SQL workspace<span class="external">↗</span></button>
        <button data-tab="logs" class="nav-item ${tab === "logs" ? "active" : ""}">${icon("logs")}Logs</button>
      </nav>
      <div class="nav-label second">APPLICATION</div>
      <nav aria-label="Application navigation">
        <button data-tab="settings" class="nav-item ${tab === "settings" ? "active" : ""}">${icon("settings")}Settings</button>
        <button data-tab="about" class="nav-item ${tab === "about" ? "active" : ""}">${icon("info")}About Hardcharger</button>
      </nav>
      <div class="sidebar-bottom"><span class="connection"><span class="nav-dot ${running ? "running" : ""}"></span>${running ? "Local server running" : "Local server stopped"}</span><a class="by-marketing" data-marketing-link href="https://gizmodata.com" target="_blank" rel="noopener noreferrer">by <strong>GizmoData™</strong></a></div>
    </aside>
    <main>
      <header><span>Hardcharger <span class="slash">/</span> ${tab === "server" ? "Local server" : tab === "about" ? "About" : tab[0].toUpperCase() + tab.slice(1)}</span><div class="header-right"><span class="pill">${designPreview ? "Design preview · sample status" : "On this computer"}</span><button id="theme" class="theme-button" aria-label="Change color theme" title="Theme: ${theme}">${theme === "dark" ? "☾" : theme === "light" ? "☀" : "◐"}</button></div></header>
      <div class="content">
        <div id="error" role="alert" ${error || s?.error ? "" : "hidden"}>${esc(error || s?.error || "")}</div>
        ${tab === "server" ? serverView(s) : tab === "settings" ? settingsView(s) : tab === "logs" ? logsView() : aboutView()}
      </div>
      <footer><span>${esc(s?.version ?? "Hardcharger")} <span class="footer-dot">·</span> Warp SQL</span><span>Fast, secure, remote DuckDB.</span></footer>
    </main>`;
  app.querySelectorAll<HTMLButtonElement>("[data-tab]").forEach(b => b.addEventListener("click", () => { tab = b.dataset.tab!; error = ""; render(); if (tab === "logs") void refreshLogs(); }));
  app.querySelectorAll<HTMLButtonElement>("[data-action]").forEach(b => b.addEventListener("click", () => void action(b.dataset.action!)));
  app.querySelectorAll<HTMLAnchorElement>("[data-marketing-link]").forEach(link => link.addEventListener("click", e => {
    if (!native) return;
    e.preventDefault();
    void invoke("open_marketing_link", { url: link.getAttribute("href") }).catch(showError);
  }));
  document.querySelector("#theme")?.addEventListener("click", () => {
    theme = theme === "system" ? "dark" : theme === "dark" ? "light" : "system";
    localStorage.setItem("hardcharger-desktop:theme", theme);
    document.documentElement.dataset.theme = theme;
    render();
  });
  document.querySelector("#settings-form")?.addEventListener("submit", e => {
    e.preventDefault();
    if (busy) return;
    const data = new FormData(e.target as HTMLFormElement);
    try {
      const options: Record<string, unknown> = {};
      for (const option of schema.filter(o => !o.managed && o.name !== "port")) {
        if (data.get(`use:${option.name}`) !== "on") continue;
        const text = String(data.get(`option:${option.name}`) ?? "");
        const choices = optionChoices(option);
        if (choices?.length && choiceValue(option, text) === undefined)
          throw new Error(`${option.name} must be one of: ${choices.join(", ")}.`);
        if (option.secret && text === "" && current?.settings.server_options[option.name] === "__HARDCHARGER_STORED_SECRET__") {
          options[option.name] = "__HARDCHARGER_STORED_SECRET__";
        } else if (option.type === "integer") {
          const value = Number(text);
          if (!text.trim() || !Number.isSafeInteger(value)) throw new Error(`${option.name} must be an integer.`);
          options[option.name] = value;
        } else if (option.type === "boolean") options[option.name] = text === "true";
        else if (option.type === "list") options[option.name] = text.split("\n").filter(v => v !== "");
        else options[option.name] = text;
      }
      const previous = current?.settings.network ?? defaultNetwork;
      const password = String(data.get("network-password") ?? "");
      const network: NetworkSettings = {
        enabled: data.get("network-mode") === "network",
        listen_address: String(data.get("network-listen") ?? previous.listen_address).trim(),
        server_name: String(data.get("network-name") ?? previous.server_name).trim(),
        tls: data.get("network-transport") === "tls",
        tls_cert: String(data.get("network-cert") ?? previous.tls_cert).trim(),
        tls_key: String(data.get("network-key") ?? previous.tls_key).trim(),
        username: String(data.get("network-user") ?? previous.username).trim(),
        password: password || (previous.password === "__HARDCHARGER_STORED_SECRET__" ? previous.password : ""),
      };
      validateNetwork(network);
      void saveSettings({ port: Number(data.get("port")), start_on_open: data.get("start") === "on", server_options: options, network });
    } catch (e) { showFormError(e); }
  });
  const updateNetwork = () => {
    const fields = document.querySelector<HTMLElement>("#network-fields");
    if (fields) fields.hidden = document.querySelector<HTMLSelectElement>("#network-mode")?.value !== "network";
    const tls = document.querySelector<HTMLSelectElement>("#network-transport")?.value === "tls";
    const certificates = document.querySelector<HTMLElement>("#network-tls-fields");
    if (certificates) certificates.hidden = !tls;
    const plain = document.querySelector<HTMLElement>("#network-plaintext-note");
    if (plain) plain.hidden = tls;
  };
  document.querySelector("#network-mode")?.addEventListener("change", updateNetwork);
  document.querySelector("#network-transport")?.addEventListener("change", updateNetwork);
  updateNetwork();
  document.querySelectorAll<HTMLInputElement>("[data-override]").forEach(input => input.addEventListener("change", () => {
    const field = document.getElementById(`option:${input.dataset.override}`) as HTMLInputElement;
    if (field) field.disabled = !input.checked;
  }));
  const search = document.querySelector<HTMLInputElement>("#settings-search");
  search?.addEventListener("input", () => { settingsSearch = search.value; filterOptions(); });
  search?.addEventListener("keydown", e => { if (e.key === "Enter") e.preventDefault(); });
  document.querySelector("#clear-settings-search")?.addEventListener("click", () => {
    settingsSearch = "";
    if (search) { search.value = ""; search.focus(); }
    filterOptions();
  });
  filterOptions();
  document.querySelector("#import-yaml")?.addEventListener("change", e => void importYaml((e.target as HTMLInputElement).files?.[0]));
  document.querySelector("#choose-import")?.addEventListener("click", () => document.querySelector<HTMLInputElement>("#import-yaml")?.click());
  document.querySelector("#export-yaml")?.addEventListener("click", exportYaml);
  document.querySelector("#apply-import")?.addEventListener("click", () => {
    if (!pendingImport || !current) return;
    const options = { ...pendingImport };
    const port = "port" in options ? Number(options.port) : current.settings.port;
    delete options.port;
    const preserved = Object.fromEntries(Object.entries(current.settings.server_options).filter(([key]) => schema.find(o => o.name === key)?.secret));
    void saveSettings({ ...current.settings, port, server_options: { ...preserved, ...options } });
  });
  document.querySelector("#cancel-import")?.addEventListener("click", () => { pendingImport = null; render(); });
  document.querySelector("#launch-login")?.addEventListener("change", e => {
    const requested = (e.target as HTMLInputElement).checked;
    void invoke<boolean>("launch_at_login", { enabled: requested }).then(v => { login = v; }).catch(showError);
  });
}

function serverView(s: Status | null) {
  const running = s?.running ?? false;
  return `<div class="page-heading"><div><p class="eyebrow">YOUR DATA, ON YOUR DESKTOP</p><h1>Local server</h1><p>One place to run your server and explore your data.</p></div></div>
    <section class="hero card">
      <div class="hero-content"><div class="status-label ${running ? "online" : "offline"}"><span class="nav-dot ${running ? "running" : ""}"></span>${busy ? "Working…" : running ? "RUNNING" : s ? "STOPPED" : "CONNECTING"}</div>
        <h2>${running ? "Ready for your next query." : "Your engine. Ready to go."}</h2>
        <p>${running ? "Your local Hardcharger server is up. Open the SQL workspace to query, import, and explore." : "Start Hardcharger to open your database and work with the full SQL workspace."}</p>
        <div class="hero-actions">${running ? button("workspace", "Open SQL workspace", "arrow", true) : button("start", "Start server", "play", true, !s)}<span class="private-note">${icon("shield")}${s?.settings.network?.enabled ? s.settings.network.tls ? "Network access · TLS" : "Network access · HTTP" : "Local connections only"}</span></div>
      </div>${picture("mascot", "Hardcharger charging bull mascot", "hero-mascot")}
    </section>
    <div class="stats"><section class="card stat"><span class="stat-label">SERVER ADDRESS</span><strong class="mono" title="${esc(s?.endpoint ?? "")}">${esc(s?.endpoint ?? "Loading…")}</strong><span>Warp SQL endpoint${s?.settings.network?.enabled ? ` · listening on ${esc(s.settings.network.listen_address)}` : ""}</span></section><section class="card stat"><span class="stat-label">UPTIME</span><strong>${running ? uptime(s!.uptimeSeconds) : "—"}</strong><span>${running ? "Since the last start" : "Server is stopped"}</span></section><section class="card stat"><span class="stat-label">DATABASE SIZE</span><strong>${s ? bytes(s.databaseBytes) : "—"}</strong><span>Stored on this computer</span></section></div>
    ${s?.settings.network?.enabled ? `<section class="card network-connection"><h3>Connect from another computer</h3><p>SQL workspace: <code>${esc(s.endpoint)}/ui/</code></p><p>Warp SQL: <code>${esc(s.endpoint)}/warp</code> · user <code>${esc(s.settings.network.username)}</code></p><p class="muted">Use your saved network password. This computer must be awake; allow inbound TCP port ${s.port} in its firewall if needed.</p></section>` : ""}
    <section class="card detail-card"><div class="section-heading"><div><h3>Server controls</h3><p>Manage the server running on this computer.</p></div><span class="pill">${running ? `PID ${s?.pid}` : "Offline"}</span></div><div class="control-actions">${button("start", "Start", "play", false, running || !s)}${button("stop", "Stop", "stop", false, !running)}${button("restart", "Restart", "restart", false, !running)}<span class="control-note">Stopping closes the SQL workspace.</span></div><div class="database-row">${icon("folder")}<div><strong>Hardcharger.duckdb</strong><span title="${esc(s?.database ?? "")}">${esc(s?.database ?? "Loading database location…")}</span></div>${button("folder", "Show folder", "folder")}</div></section>
    <div class="resident-note">${icon("info")}<p><strong>Close the window. Keep the engine running.</strong> Hardcharger stays in your menu bar or system tray. Choose <strong>Quit Hardcharger</strong> there to stop the server and exit.</p></div>`;
}

function settingsView(s: Status | null) {
  return `<div class="page-heading"><div><p class="eyebrow">MAKE IT YOURS</p><h1>Settings</h1><p>A few preferences for your local server.</p></div></div>
    ${settingsNotice ? `<p class="settings-notice" role="status">${esc(settingsNotice)}</p>` : ""}
    <section class="card detail-card"><div class="section-heading"><div><h3>Configuration YAML</h3><p>Import server settings, or export a configuration for another machine.</p></div></div><div class="yaml-actions"><button id="choose-import" class="button" ${s?.running || !native || busy ? "disabled" : ""}>${icon("folder")}Import YAML</button><input id="import-yaml" type="file" accept=".yaml,.yml,.json" hidden><button id="export-yaml" class="button" ${!s ? "disabled" : ""}>Export YAML</button><span class="muted">Exports leave out passwords and other secrets.</span></div>${pendingImport ? `<div class="import-review"><h3>Review imported settings</h3><p class="muted">These settings replace your custom values. Existing stored secrets are kept unless the file supplies replacements. The server validates the configuration when it starts.</p><pre>${esc(stringify(Object.fromEntries(Object.entries(pendingImport).map(([key, value]) => [key, schema.find(o => o.name === key)?.secret ? "[secret supplied]" : value]))))}</pre><button id="apply-import" class="button primary">Apply imported settings</button> <button id="cancel-import" class="button">Cancel</button></div>` : ""}</section>
    <form id="settings-form" class="card detail-card"><div class="section-heading"><div><h3>Local server</h3><p>${s?.running ? "Stop the server to change these settings." : "Changes apply the next time the server starts."}</p></div></div>
      <label class="field"><span>Port</span><input name="port" type="number" min="1024" max="65535" required value="${s?.settings.port ?? 31337}" ${s?.running || !native ? "disabled" : ""}><small>Used by the SQL workspace and Warp SQL clients.</small></label>
      <label class="check-row"><input name="start" type="checkbox" ${s?.settings.start_on_open ? "checked" : ""} ${s?.running || !native ? "disabled" : ""}><span><strong>Start server when Hardcharger opens</strong><small>Your database is ready when you need it.</small></span></label>
      ${networkView(s)}
      <h3 class="options-heading">Server options</h3><p class="muted">Defaults come from this server build. Enable an override to change a value.</p>
      <div class="settings-search"><label for="settings-search">Search setting names</label><div class="settings-search-input"><input id="settings-search" type="search" placeholder="e.g. memory_limit" value="${esc(settingsSearch)}" autocomplete="off" aria-controls="server-options" aria-describedby="settings-search-status"><button id="clear-settings-search" class="button" type="button" hidden>Clear</button></div><p id="settings-search-status" class="muted" role="status" aria-live="polite"></p></div>
      <div id="server-options">
      ${optionGroups(s)}
      </div>
      <button class="button primary save-settings" type="submit" ${s?.running || !native || busy ? "disabled" : ""}>Save settings</button>
    </form>
    <section class="card detail-card"><div class="section-heading"><div><h3>Application</h3><p>Keep Hardcharger close at hand.</p></div></div><label class="check-row"><input id="launch-login" type="checkbox" ${login ? "checked" : ""} ${!native ? "disabled" : ""}><span><strong>Launch at login</strong><small>Open quietly in the menu bar or system tray when you sign in.</small></span></label><p class="muted">The window can close while the server keeps running. Quit Hardcharger to stop it cleanly.</p></section>`;
}

function networkView(s: Status | null) {
  const n = s?.settings.network ?? defaultNetwork;
  const disabled = s?.running || !native || busy ? "disabled" : "";
  return `<section class="network-settings" aria-label="Network access">
    <h3>Network access</h3><p class="muted">Choose who can reach the server on this computer. Changes apply at the next server start.</p>
    <label class="field"><span>Connections</span><select id="network-mode" name="network-mode" aria-label="Connections" ${disabled}><option value="local" ${!n.enabled ? "selected" : ""}>Only this computer</option><option value="network" ${n.enabled ? "selected" : ""}>Allow network connections</option></select></label>
    <div id="network-fields" ${!n.enabled ? "hidden" : ""}>
      <div class="network-grid">
        <label class="field"><span>Listen IP address</span><input name="network-listen" aria-label="Listen IP address" value="${esc(n.listen_address)}" placeholder="0.0.0.0" ${disabled}><small>0.0.0.0 listens on all IPv4 interfaces; use a specific LAN IP to limit it.</small></label>
        <label class="field"><span>Server DNS name or IP</span><input name="network-name" aria-label="Server DNS name or IP" value="${esc(n.server_name)}" placeholder="my-mac.local or 192.168.1.20" ${disabled}><small>The address clients use. It must resolve to this computer and match the TLS certificate.</small></label>
        <label class="field"><span>Transport</span><select id="network-transport" name="network-transport" aria-label="Transport" ${disabled}><option value="tls" ${n.tls ? "selected" : ""}>TLS — encrypted (recommended)</option><option value="http" ${!n.tls ? "selected" : ""}>HTTP — unencrypted private network</option></select></label>
      </div>
      <p id="network-plaintext-note" class="network-warning" ${n.tls ? "hidden" : ""}>HTTP sends passwords, queries, and results without encryption. Use it only on a trusted private network.</p>
      <div id="network-tls-fields" ${!n.tls ? "hidden" : ""}>
        <div class="network-grid">
          <label class="field"><span>TLS certificate chain (PEM path)</span><input name="network-cert" aria-label="TLS certificate chain (PEM path)" value="${esc(n.tls_cert)}" placeholder="/path/to/server.pem" ${disabled}></label>
          <label class="field"><span>TLS private key (PEM path)</span><input name="network-key" aria-label="TLS private key (PEM path)" value="${esc(n.tls_key)}" placeholder="/path/to/server.key" ${disabled}></label>
        </div>
        <p class="muted">Certificate verification stays enabled. For the desktop SQL workspace and client browsers, trust a private CA in the operating system, or use a certificate already trusted there. Private keys stay in their files.</p>
      </div>
      <div class="network-grid">
        <label class="field"><span>Administrator user name</span><input name="network-user" aria-label="Administrator user name" value="${esc(n.username)}" autocomplete="off" ${disabled}></label>
        <label class="field"><span>Administrator password</span><input name="network-password" aria-label="Administrator password" type="password" autocomplete="new-password" placeholder="${n.password ? "Saved in OS credential store — leave blank to keep" : "Set a strong password"}" ${disabled}><small>Stored in the OS credential store. Other users can sign in through the server's configured access control or identity provider.</small></label>
      </div>
    </div>
  </section>`;
}

function optionGroups(s: Status | null) {
  const groups = [...new Set(schema.map(o => o.group))];
  return groups.map(group => `<details class="option-group"><summary>${esc(group)}<span></span></summary>${schema.filter(o => o.group === group).map(option => {
    if (option.name === "port") return "";
    const value = s?.settings.server_options[option.name];
    const custom = value !== undefined;
    const stored = value === "__HARDCHARGER_STORED_SECRET__";
    const text = stored ? "" : value !== undefined ? Array.isArray(value) ? value.join("\n") : String(value) : option.default;
    const disabled = s?.running || !native || option.managed;
    let input = "";
    const id = `option:${option.name}`;
    const choices = optionChoices(option);
    const selected = choices?.length ? choiceValue(option, text) : undefined;
    if (option.managed) input = `<span class="managed-label">Managed by desktop app</span>`;
    else if (choices?.length) input = `<select id="${id}" name="${id}" ${disabled || !custom ? "disabled" : ""}>${selected === undefined ? `<option value="" selected disabled>Choose a valid value${custom ? ` (saved: ${esc(text)})` : ""}</option>` : ""}${choices.map(choice => `<option value="${esc(choice)}" ${selected === choice ? "selected" : ""}>${esc(choice)}</option>`).join("")}</select>`;
    else if (option.type === "list") input = `<textarea id="${id}" name="${id}" rows="2" placeholder="One value per line" ${disabled || !custom ? "disabled" : ""}>${esc(text)}</textarea>`;
    else input = `<input id="${id}" name="${id}" type="${option.secret ? "password" : option.type === "integer" ? "number" : "text"}" value="${esc(text)}" placeholder="${stored ? "Saved in OS credential store" : "Server default"}" autocomplete="off" ${disabled || !custom ? "disabled" : ""}>`;
    return `<div class="server-option" data-option-name="${esc(option.name)}"><div class="option-description"><label for="${id}">${esc(option.name)}</label><p>${esc(option.description)}</p>${option.secret && !option.managed ? '<small>Stored in your OS credential store.</small>' : ""}</div><div class="option-value">${!option.managed ? `<label class="override"><input name="use:${option.name}" data-override="${option.name}" type="checkbox" ${custom ? "checked" : ""} ${disabled ? "disabled" : ""}>Override</label>` : ""}${input}</div></div>`;
  }).join("")}</details>`).join("");
}

function filterOptions() {
  const query = settingsSearch.trim().toLowerCase();
  let total = 0, matches = 0;
  // Hide existing rows instead of rebuilding the form, preserving unsaved values.
  app.querySelectorAll<HTMLDetailsElement>(".option-group").forEach(group => {
    const rows = Array.from(group.querySelectorAll<HTMLElement>("[data-option-name]"));
    let visible = 0;
    rows.forEach(row => {
      row.hidden = !row.dataset.optionName!.toLowerCase().includes(query);
      if (!row.hidden) visible++;
    });
    total += rows.length;
    matches += visible;
    group.hidden = visible === 0;
    const count = group.querySelector("summary span");
    if (count) count.textContent = query ? `${visible} of ${rows.length} options` : `${rows.length} options`;
    if (query) {
      group.dataset.beforeSearch ??= String(group.open);
      group.open = visible > 0;
    } else if (group.dataset.beforeSearch !== undefined) {
      group.open = group.dataset.beforeSearch === "true";
      delete group.dataset.beforeSearch;
    }
  });
  const status = document.querySelector("#settings-search-status");
  if (status) status.textContent = query
    ? matches ? `${matches} of ${total} settings match.` : "No settings match. Try another name or clear the search."
    : `${total} settings. Search by any part of a setting name.`;
  const clear = document.querySelector<HTMLButtonElement>("#clear-settings-search");
  if (clear) clear.hidden = settingsSearch.length === 0;
}

async function importYaml(file: File | undefined) {
  if (!file || !current) return;
  try {
    if (file.size > 1024 * 1024) throw new Error("Configuration files must be smaller than 1 MB.");
    pendingImport = parseConfiguration(await file.text(), schema); settingsNotice = ""; error = ""; render();
  } catch (e) { showError(e); }
}

function exportYaml() {
  if (!current) return;
  const yaml = exportConfiguration(current.port, current.settings.server_options, schema);
  const blob = new Blob([yaml], { type: "application/yaml" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a"); a.href = url; a.download = "hardcharger.yaml"; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function logsView() {
  return `<div class="page-heading"><div><p class="eyebrow">UNDER THE HOOD</p><h1>Server logs</h1><p>Startup, connections, and server activity.</p></div>${button("logs", "Refresh", "restart")}</div><section class="card log-card"><pre id="log-output" tabindex="0">${designPreview ? "Design preview. Live server logs appear in the desktop app." : "Loading logs…"}</pre></section>`;
}

function aboutView() {
  return `<section class="card about-card" aria-label="About Hardcharger">${picture("mascot", "Hardcharger™", "about-mascot")}<p class="about-tagline">Fast, secure, remote DuckDB.</p><p>${esc(current?.version ?? "Desktop preview")} · by GizmoData™</p><div class="about-divider"></div><p>The Hardcharger server and its SQL workspace,<br>together on your desktop.</p><div class="marketing-links"><a class="button primary" data-marketing-link href="https://gizmodata.com/hardcharger" target="_blank" rel="noopener noreferrer">Explore Hardcharger ${icon("arrow")}</a><a class="button" data-marketing-link href="https://gizmodata.com" target="_blank" rel="noopener noreferrer">Visit GizmoData ${icon("arrow")}</a></div><p class="muted">Apache License 2.0<br>Hardcharger™ and GizmoData™ are trademarks of GizmoData LLC.</p></section>`;
}

function showError(e: unknown) { error = String(e); render(); }

function showFormError(e: unknown) {
  error = String(e);
  const alert = document.querySelector<HTMLElement>("#error");
  if (alert) { alert.textContent = error; alert.hidden = false; alert.scrollIntoView({ block: "nearest" }); }
}

async function refresh() {
  if (!native || busy) return;
  try {
    const s = await invoke<Status>("server_status");
    const changed = !current || current.running !== s.running || current.error !== s.error;
    current = s;
    // Keep form focus, scroll position, and text selections while polling.
    if (changed) render();
    else if (tab === "server") {
      const values = app.querySelectorAll<HTMLElement>(".stat strong");
      if (values[1]) values[1].textContent = s.running ? uptime(s.uptimeSeconds) : "—";
      if (values[2]) values[2].textContent = bytes(s.databaseBytes);
    }
  } catch (e) { showError(e); }
}

async function refreshLogs() {
  if (!native) return;
  try {
    const logs = await invoke<string>("read_logs");
    const output = document.querySelector("#log-output");
    if (output) { output.textContent = logs; output.scrollTop = output.scrollHeight; }
  } catch (e) { showError(e); }
}

async function action(name: string) {
  if (!native || busy) return;
  busy = true; error = ""; render();
  try {
    if (["start", "stop", "restart"].includes(name)) current = await invoke<Status>("server_control", { action: name });
    else if (name === "workspace") await invoke("open_workspace");
    else if (name === "folder") await invoke("reveal_data");
    else if (name === "logs") await refreshLogs();
  } catch (e) { error = String(e); }
  finally { busy = false; render(); if (tab === "logs") void refreshLogs(); }
}

async function saveSettings(settings: Settings) {
  busy = true; error = "";
  try { current = await invoke<Status>("save_settings", { settings }); pendingImport = null; settingsNotice = "Settings saved. They take effect the next time the server starts."; }
  catch (e) { busy = false; showFormError(e); return; }
  finally { busy = false; }
  render();
}

if (designPreview) {
  current = { running: true, pid: 24781, port: 31337, endpoint: "http://127.0.0.1:31337", database: "~/Library/Application Support/Hardcharger/Hardcharger.duckdb", logFile: "server.log", uptimeSeconds: 165, databaseBytes: 122683392, version: "Hardcharger 2.0.0", error: null, settings: { port: 31337, start_on_open: true, server_options: {} } };
} else if (!native) {
  error = "Open the Hardcharger desktop app to control your server. Use ?preview=1 to view the design in a browser.";
}
render();
if (native) {
  void invoke<ServerOption[]>("server_options").then(options => { schema = options; if (tab === "settings") render(); }).catch(showError);
  void listen("server-changed", () => void refresh());
  void listen<string>("desktop-error", e => showError(e.payload));
  void invoke<boolean>("launch_at_login", { enabled: null }).then(v => { login = v; if (tab === "settings") render(); }).catch(showError);
  void refresh();
  setInterval(() => { void refresh(); if (tab === "logs" && !busy) void refreshLogs(); }, 2000);
}
