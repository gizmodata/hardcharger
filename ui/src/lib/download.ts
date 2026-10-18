// Saves a Blob as a file through a temporary link.

export function saveFile(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.rel = "noopener";
  a.hidden = true;
  // A click that bubbles reaches page-wide listeners (the editor's, in Safari, turns it into
  // a clipboard write the browser refuses); the link's own default action is all we want.
  a.addEventListener("click", (e) => e.stopPropagation());
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Safari reads the URL after click() returns: revoke it later, not now.
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

/** A file name for an export: results-YYYYMMDD-HHMMSS.ext (local time). */
export function exportName(extension: string, now = new Date()): string {
  const p = (n: number) => String(n).padStart(2, "0");
  const stamp = `${now.getFullYear()}${p(now.getMonth() + 1)}${p(now.getDate())}-${p(now.getHours())}${p(now.getMinutes())}${p(now.getSeconds())}`;
  return `results-${stamp}.${extension}`;
}
