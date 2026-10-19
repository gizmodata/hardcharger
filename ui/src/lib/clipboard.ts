// Copy to the clipboard in a way every browser allows, Safari included.
//
// Safari refuses the asynchronous Clipboard API from a key handler (Cmd+C), so the copy goes
// through the browser's own copy command: a scratch selection, document.execCommand("copy"),
// and a copy-event listener on window, in the capture phase, that fills the clipboard with
// our text (so no other copy handler on the page, like the editor's, can replace it). The
// Clipboard API is the fallback where the copy command is refused.

let pending: { text: string; html: string | undefined } | undefined;

function onCopy(event: ClipboardEvent): void {
  if (pending === undefined || !event.clipboardData) return;
  event.clipboardData.setData("text/plain", pending.text);
  if (pending.html !== undefined) event.clipboardData.setData("text/html", pending.html);
  event.preventDefault();
  event.stopImmediatePropagation();
  pending = undefined;
}

let installed = false;
function install(): void {
  if (installed) return;
  installed = true;
  window.addEventListener("copy", onCopy, { capture: true });
}

/**
 * Copies `text` (and, when given, `html`: a spreadsheet takes a table's cells from it). Resolves
 * to true once it is on the clipboard.
 */
export async function copyText(text: string, html?: string): Promise<boolean> {
  install();
  const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const scratch = document.createElement("textarea");
  scratch.value = text || " ";
  scratch.setAttribute("readonly", "");
  scratch.setAttribute("aria-hidden", "true");
  scratch.className = "clipboard-scratch";
  document.body.appendChild(scratch);
  scratch.select();
  pending = { text, html };
  let copied: boolean;
  try {
    // Deprecated, and still the only copy Safari allows from a key handler.
    // eslint-disable-next-line @typescript-eslint/no-deprecated
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  }
  // The listener clears `pending` when the copy event ran; if it is still set, the command
  // was refused without an event.
  if (pending !== undefined) copied = false;
  pending = undefined;
  scratch.remove();
  active?.focus({ preventScroll: true });
  if (copied) return true;
  try {
    if (html !== undefined && typeof ClipboardItem !== "undefined" && navigator.clipboard.write) {
      await navigator.clipboard.write([
        new ClipboardItem({
          "text/plain": new Blob([text], { type: "text/plain" }),
          "text/html": new Blob([html], { type: "text/html" }),
        }),
      ]);
    } else {
      await navigator.clipboard.writeText(text);
    }
    return true;
  } catch {
    return false;
  }
}
