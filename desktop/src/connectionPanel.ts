import { connectionExamples } from "./connectionExamples";

interface ConnectionStatus {
  running: boolean; port: number; endpoint: string;
  settings: { network?: { enabled: boolean; username: string; tls: boolean } };
}
let selectedTab = "web";
let copied = "";
let context = "";
const esc = (value: string) => value.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

export function connectionPanel(status: ConnectionStatus | null): string {
  if (!status?.settings.network?.enabled) return "";
  const network = status.settings.network;
  let details: ReturnType<typeof connectionExamples>;
  try { details = connectionExamples(status.endpoint, network.username); }
  catch { return '<section class="card network-connection"><h3>Connect from another computer</h3><p>Save valid network settings to generate connection examples.</p></section>'; }
  const nextContext = `${status.endpoint}:${network.username}`;
  if (context !== nextContext) { context = nextContext; copied = ""; }
  const selected = details.examples.find(example => example.id === selectedTab) ?? details.examples[0];
  return `<section class="card network-connection" aria-label="Connect from another computer">
    <div class="section-heading"><div><h3>Connect from another computer</h3><p>Share connection details and examples. Passwords are never included.</p></div><span class="pill">${status.running ? "Server running" : "Server stopped"}</span></div>
    <div class="connection-uri"><code>${esc(details.uri)}</code><button class="button" data-copy-connection="uri">Copy URI</button></div>
    <div class="connection-tabs" role="tablist" aria-label="Connection examples">${details.examples.map(example => `<button id="connect-tab-${example.id}" role="tab" aria-selected="${example.id === selected.id}" aria-controls="connect-panel-${example.id}" tabindex="${example.id === selected.id ? "0" : "-1"}" data-connection-tab="${example.id}">${esc(example.label)}</button>`).join("")}</div>
    <div id="connect-panel-${selected.id}" role="tabpanel" aria-labelledby="connect-tab-${selected.id}"><p class="connection-setup">${esc(selected.setup)}</p><div class="connection-code-heading"><a data-marketing-link href="${esc(selected.docs)}" target="_blank" rel="noopener noreferrer">Client documentation ↗</a><button class="button" data-copy-connection="example">${selected.id === "web" ? "Copy web address" : "Copy example"}</button></div><pre class="connection-code" tabindex="0" aria-label="${esc(selected.label)} connection example"><code>${esc(selected.code)}</code></pre></div>
    <p id="connection-copy-status" class="muted" role="status" aria-live="polite">${esc(copied)}</p>
    <p class="muted">Set HARDCHARGER_USER and HARDCHARGER_PASSWORD on the client for its own account. Examples default to user <code>${esc(network.username)}</code>.</p>
    ${network.tls ? '<p class="muted">For a generated or private CA, copy its public certificate to the client and set HARDCHARGER_TLS_ROOTS to that client’s PEM file path (CLI: --tls-roots). Browser clients must trust the CA on their own computer. Certificate verification stays enabled.</p>' : '<p class="network-warning">This server uses HTTP without encryption. Examples explicitly disable TLS.</p>'}
    <p class="muted">This computer must be awake; allow inbound TCP port ${status.port} in its firewall if needed.${status.running ? "" : " Start the server before connecting."}</p>
  </section>`;
}

export function bindConnectionPanel(status: ConnectionStatus | null, render: () => void): void {
  if (!status?.settings.network?.enabled) return;
  const select = (id: string) => {
    selectedTab = id; copied = ""; render();
    document.querySelector<HTMLButtonElement>(`[data-connection-tab="${id}"]`)?.focus();
  };
  document.querySelectorAll<HTMLButtonElement>("[data-connection-tab]").forEach(button => {
    button.addEventListener("click", () => select(button.dataset.connectionTab!));
    button.addEventListener("keydown", event => {
      const tabs = Array.from(document.querySelectorAll<HTMLButtonElement>("[data-connection-tab]"));
      const index = tabs.indexOf(button);
      const next = event.key === "ArrowRight" ? (index + 1) % tabs.length : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : undefined;
      if (next !== undefined) { event.preventDefault(); select(tabs[next].dataset.connectionTab!); }
    });
  });
  document.querySelectorAll<HTMLButtonElement>("[data-copy-connection]").forEach(button => button.addEventListener("click", () => {
    const details = connectionExamples(status.endpoint, status.settings.network!.username);
    const selected = details.examples.find(example => example.id === selectedTab) ?? details.examples[0];
    void copyText(button.dataset.copyConnection === "uri" ? details.uri : selected.code).then(ok => {
      copied = ok ? "Copied to clipboard." : "Select the example and copy it with your keyboard.";
      const message = document.querySelector("#connection-copy-status");
      if (message) message.textContent = copied;
    });
  }));
}

async function copyText(text: string): Promise<boolean> {
  // Preserve Safari/WKWebView's user activation with a synchronous copy first.
  const area = document.createElement("textarea");
  area.value = text; area.className = "connection-copy-scratch"; area.setAttribute("aria-hidden", "true");
  const focused = document.activeElement as HTMLElement | null;
  document.body.append(area); area.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { /* try the Clipboard API */ }
  area.remove(); focused?.focus({ preventScroll: true });
  if (ok) return true;
  try { await navigator.clipboard.writeText(text); return true; } catch { return false; }
}
