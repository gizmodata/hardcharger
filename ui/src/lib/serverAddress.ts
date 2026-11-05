// A server's address as the Add a server dialog holds it (host, port, TLS, an optional base
// path), what a pasted URI fills in, and the URL the relay is given.
//
// The URIs people paste (the forms Hardcharger's clients take):
//
//   hardcharger://HOST[:PORT][/PREFIX][?tls=false]   port 31337 by default (443 for a GizmoData Cloud
//                                                    host), TLS unless tls=false
//   chargewire://HOST[:PORT][/PREFIX][?tls=false]    the same
//   jdbc:hardcharger://HOST[:PORT][/CATALOG][?k=v]   the JDBC driver's form: its path is a catalog
//   https://HOST[:PORT][/PREFIX]                     port 443 by default
//   http://HOST[:PORT][/PREFIX]                      port 80 by default, no TLS
//
// The PREFIX is where the server mounts Chargewire (default /chargewire, spec §2.1). User info
// fills the user name; a password in a URI is dropped, never kept.

export const defaultPort = 31337;

/** The port GizmoData Cloud's Hardcharger clusters answer on. */
export const gizmoDataCloudPort = 443;

/**
 * Whether `host` is a GizmoData Cloud host: gizmodata.com or *.gizmodata.com (any case, a
 * trailing dot allowed), on the domain boundary (not evilgizmodata.com).
 */
export function isGizmoDataCloudHost(host: string): boolean {
  const h = host.trim().toLowerCase().replace(/\.$/, "");
  return h === "gizmodata.com" || (h.endsWith(".gizmodata.com") && h.length > ".gizmodata.com".length);
}

/** The port to assume for `host` when none is given: 443 on GizmoData Cloud, else 31337. */
export function defaultPortFor(host: string): number {
  return isGizmoDataCloudHost(host) ? gizmoDataCloudPort : defaultPort;
}

export interface ServerAddress {
  host: string;
  port: number;
  tls: boolean;
  /** Where Chargewire is mounted, "" for the default (/chargewire); starts with "/" otherwise. */
  prefix: string;
}

export interface ParsedAddress extends ServerAddress {
  /** The user name the URI carried, if any. */
  username?: string;
  /** A JDBC URI's catalog, if any (a default catalog, not a path). */
  catalog?: string;
  /** Whether the URI named the port (else it is the scheme's or the host's default). */
  portGiven?: boolean;
}

export type ParseResult = { ok: true; address: ParsedAddress } | { ok: false; error: string };

/** Whether `text` looks like a URI (so the Host field should be read as one). */
export function looksLikeUri(text: string): boolean {
  return /^\s*(jdbc:)?[a-z][a-z0-9+.-]*:\/\//i.test(text);
}

/** Reads a pasted URI into the dialog's fields. */
export function parseServerUri(text: string): ParseResult {
  let rest = text.trim();
  const fail = (error: string): ParseResult => ({ ok: false, error });
  let jdbc = false;
  if (/^jdbc:/i.test(rest)) {
    jdbc = true;
    rest = rest.slice(5);
  }
  const schemeEnd = rest.indexOf("://");
  if (schemeEnd <= 0) return fail("Not a server URI: it needs a scheme, e.g. hardcharger://host:31337");
  const scheme = rest.slice(0, schemeEnd).toLowerCase();
  rest = rest.slice(schemeEnd + 3);
  let tls = true;
  let port = defaultPort;
  let tlsParameter = false;
  if (jdbc) {
    if (scheme !== "hardcharger") return fail("A JDBC URI here must start with jdbc:hardcharger://");
  } else if (scheme === "hardcharger" || scheme === "chargewire") {
    tlsParameter = true;
  } else if (scheme === "https") {
    port = 443;
  } else if (scheme === "http") {
    tls = false;
    port = 80;
  } else {
    return fail(`${scheme}:// isn't a server URI here: use hardcharger://, chargewire://, https:// or http://`);
  }
  const hash = rest.indexOf("#");
  if (hash >= 0) rest = rest.slice(0, hash);
  let query = "";
  const q = rest.indexOf("?");
  if (q >= 0) {
    query = rest.slice(q + 1);
    rest = rest.slice(0, q);
  }
  let path = "";
  const slash = rest.indexOf("/");
  if (slash >= 0) {
    path = rest.slice(slash);
    rest = rest.slice(0, slash);
  }
  let authority = rest;
  let username: string | undefined;
  const at = authority.lastIndexOf("@");
  if (at >= 0) {
    const userInfo = authority.slice(0, at);
    authority = authority.slice(at + 1);
    const colon = userInfo.indexOf(":");
    const user = colon >= 0 ? userInfo.slice(0, colon) : userInfo;
    try {
      username = decodeURIComponent(user) || undefined;
    } catch {
      username = user || undefined;
    }
  }
  let host: string;
  let portText = "";
  if (authority.startsWith("[")) {
    const close = authority.indexOf("]");
    if (close < 0) return fail("Not a server URI: the [ around an IPv6 address isn't closed");
    host = authority.slice(1, close);
    const after = authority.slice(close + 1);
    if (after) {
      if (!after.startsWith(":")) return fail("Not a server URI: unexpected text after the IPv6 address");
      portText = after.slice(1);
    }
  } else {
    const colon = authority.indexOf(":");
    host = colon >= 0 ? authority.slice(0, colon) : authority;
    if (colon >= 0) portText = authority.slice(colon + 1);
  }
  if (!host) return fail("Not a server URI: it names no host");
  if (/[\s/\\]/.test(host)) return fail("Not a server URI: the host has a character a host can't have");
  if (portText) {
    if (!/^\d{1,5}$/.test(portText) || Number(portText) < 1 || Number(portText) > 65535) {
      return fail(`Not a server URI: "${portText}" is not a port`);
    }
    port = Number(portText);
  } else if ((jdbc || scheme === "hardcharger") && isGizmoDataCloudHost(host)) {
    port = gizmoDataCloudPort; // GizmoData Cloud's clusters answer on 443
  }
  const params = new URLSearchParams(query);
  if (tlsParameter || jdbc) {
    const value = params.get("tls") ?? params.get("useEncryption") ?? params.get("ssl");
    if (value !== null && value !== "") {
      const v = value.toLowerCase();
      if (v === "false" || v === "0") tls = false;
      else if (v === "true" || v === "1") tls = true;
      else return fail(`Not a server URI: tls=${value} (want true or false)`);
    }
  }
  const address: ParsedAddress = { host, port, tls, prefix: "", portGiven: portText !== "" };
  if (jdbc) {
    const catalog = path.replace(/^\/+|\/+$/g, "");
    if (catalog) address.catalog = safeDecode(catalog);
  } else {
    const prefix = path.replace(/\/+$/, "");
    if (prefix && prefix !== "/chargewire") address.prefix = prefix;
  }
  if (username) address.username = username;
  return { ok: true, address };
}

function safeDecode(text: string): string {
  try {
    return decodeURIComponent(text);
  } catch {
    return text;
  }
}

/** The host as it goes in a URL: IPv6 addresses in brackets. */
export function urlHost(host: string): string {
  return host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
}

/** The URL the relay is given for `address`. */
export function serverUrl(address: ServerAddress): string {
  return `${address.tls ? "https" : "http"}://${urlHost(address.host)}:${address.port}${address.prefix}`;
}

/** host:port, for people. */
export function addressLabel(address: Pick<ServerAddress, "host" | "port">): string {
  return `${urlHost(address.host)}:${address.port}`;
}

/** A Host field's problem, or undefined. */
export function hostProblem(host: string): string | undefined {
  const h = host.trim();
  if (!h) return "Enter the server's host name or address.";
  if (/[\s/\\@?#]/.test(h)) return "A host is a name or an address, without a scheme, path or spaces.";
  return undefined;
}

/** A Port field's problem, or undefined. */
export function portProblem(port: number): string | undefined {
  return Number.isInteger(port) && port >= 1 && port <= 65535 ? undefined : "A port is a number from 1 to 65535.";
}

/** A prefix as stored: "" for the default, else "/path" without a trailing slash. */
export function normalPrefix(prefix: string): string {
  const p = prefix.trim().replace(/\/+$/, "");
  if (!p || p === "/chargewire") return "";
  return p.startsWith("/") ? p : `/${p}`;
}
