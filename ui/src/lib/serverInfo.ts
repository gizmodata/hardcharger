// What About lists from the server-info document (Warp SQL §10).

/**
 * The server-info keys (Warp SQL §10) the summary rows already show: Server is the server's
 * name and version, Protocol the protocol's, Instance the instance id.
 */
const summarized = new Set([
  "server_name",
  "server_version",
  "protocol",
  "protocol_version",
  "instance_id",
  "hardcharger_instance_id",
]);

/** The server-info facts the summary does not show already, in the server's order. */
export function remainingServerInfo(info: Record<string, unknown>): [string, string][] {
  const rows: [string, string][] = [];
  for (const [k, v] of Object.entries(info)) {
    if (summarized.has(k)) continue;
    if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") rows.push([k, String(v)]);
  }
  return rows;
}
