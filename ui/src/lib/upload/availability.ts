// Whether an upload can be offered: one into a session that is, or will be opened as,
// read-only can't succeed, so its entry points (the editor tab's Upload, the schema browser's
// Upload button and "Upload data…" items, a file dropped on the page) are not offered, with
// the reason. The server's refusal of an ingest in a read-only session stays the backstop.

export const uploadOffInReadOnlyTab = "Upload is off in a read-only tab";
export const uploadOffOnReadOnlyConnection = "Upload is off: this connection opens read-only sessions";

/**
 * Why an upload can't be offered now, or undefined when it can. `connectionReadOnly`: the
 * connection the upload's own session opens on is saved with "Read-only sessions" (that session
 * opens read-only). `tabReadOnly`: for an entry that follows the active tab (the schema
 * browser's Upload button, a dropped file), the tab's Read-only box is checked or pending (the
 * upload then opens read-only too).
 */
export function uploadUnavailable(options: { connectionReadOnly: boolean; tabReadOnly?: boolean }): string | undefined {
  // The connection's setting first: a tab on such a connection is read-only because of it.
  if (options.connectionReadOnly) return uploadOffOnReadOnlyConnection;
  if (options.tabReadOnly) return uploadOffInReadOnlyTab;
  return undefined;
}
