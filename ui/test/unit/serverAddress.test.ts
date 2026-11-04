import { describe, expect, it } from "vitest";

import {
  addressLabel,
  hostProblem,
  looksLikeUri,
  normalPrefix,
  parseServerUri,
  portProblem,
  serverUrl,
} from "../../src/lib/serverAddress";

const ok = (text: string) => {
  const r = parseServerUri(text);
  if (!r.ok) throw new Error(`${text}: ${r.error}`);
  return r.address;
};
const bad = (text: string) => {
  const r = parseServerUri(text);
  if (r.ok) throw new Error(`${text} parsed: ${JSON.stringify(r.address)}`);
  return r.error;
};

describe("parseServerUri", () => {
  it("reads hardcharger:// as the vanity drivers do: port 31337, TLS unless tls=false, a path prefix", () => {
    expect(ok("hardcharger://db.example.com")).toEqual({ host: "db.example.com", port: 31337, tls: true, prefix: "" });
    expect(ok("hardcharger://db.example.com:4433")).toMatchObject({ port: 4433, tls: true });
    expect(ok("hardcharger://db.example.com?tls=false")).toMatchObject({ port: 31337, tls: false });
    expect(ok("hardcharger://db.example.com?tls=0")).toMatchObject({ tls: false });
    expect(ok("HARDCHARGER://db.example.com?tls=TRUE")).toMatchObject({ tls: true });
    expect(ok("hardcharger://h:1/proxy/chargewire/")).toMatchObject({ prefix: "/proxy/chargewire" });
    expect(ok("hardcharger://h/chargewire")).toMatchObject({ prefix: "" }); // the default path
    expect(bad("hardcharger://h?tls=maybe")).toContain("tls=maybe");
  });

  it("reads chargewire:// with the same rules", () => {
    expect(ok("chargewire://10.0.0.5")).toEqual({ host: "10.0.0.5", port: 31337, tls: true, prefix: "" });
    expect(ok("chargewire://10.0.0.5:9000?tls=false")).toMatchObject({ port: 9000, tls: false });
  });

  it("reads jdbc:hardcharger://, whose path is a catalog, not a prefix", () => {
    expect(ok("jdbc:hardcharger://wh.example.com:31337/lake?useEncryption=false")).toEqual({
      host: "wh.example.com",
      port: 31337,
      tls: false,
      prefix: "",
      catalog: "lake",
    });
    expect(ok("jdbc:hardcharger://wh.example.com")).toEqual({
      host: "wh.example.com",
      port: 31337,
      tls: true,
      prefix: "",
    });
    expect(bad("jdbc:postgresql://h/db")).toContain("jdbc:hardcharger://");
  });

  it("reads https:// and http:// URLs with their own default ports", () => {
    expect(ok("https://db.example.com")).toEqual({ host: "db.example.com", port: 443, tls: true, prefix: "" });
    expect(ok("http://db.example.com")).toEqual({ host: "db.example.com", port: 80, tls: false, prefix: "" });
    expect(ok("https://db.example.com:31337/chargewire")).toMatchObject({ port: 31337, prefix: "" });
    // tls= is the vanity schemes' parameter, not a URL's.
    expect(ok("https://h?tls=false")).toMatchObject({ tls: true });
  });

  it("reads IPv6 addresses in brackets", () => {
    expect(ok("hardcharger://[::1]:31400")).toEqual({ host: "::1", port: 31400, tls: true, prefix: "" });
    expect(ok("https://[2001:db8::7]")).toEqual({ host: "2001:db8::7", port: 443, tls: true, prefix: "" });
    expect(bad("hardcharger://[::1")).toContain("IPv6");
    expect(bad("hardcharger://[::1]x")).toContain("IPv6");
  });

  it("takes a user name from the user info and never keeps a password", () => {
    const a = ok("hardcharger://joe:s3cret@db.example.com:31337");
    expect(a.username).toBe("joe");
    expect(JSON.stringify(a)).not.toContain("s3cret");
    expect(ok("hardcharger://ann%40corp@db.example.com").username).toBe("ann@corp");
    expect(ok("https://:only-a-password@h").username).toBeUndefined();
  });

  it("refuses junk with a reason", () => {
    expect(bad("")).toContain("scheme");
    expect(bad("db.example.com:31337")).toContain("scheme");
    expect(bad("ftp://h")).toContain("ftp://");
    expect(bad("hardcharger://")).toContain("no host");
    expect(bad("hardcharger://h:0")).toContain("not a port");
    expect(bad("hardcharger://h:70000")).toContain("not a port");
    expect(bad("hardcharger://h:12ab")).toContain("not a port");
    expect(bad("hardcharger+grpc://h")).toContain("isn't a server URI");
  });
});

describe("looksLikeUri", () => {
  it("tells a URI from a host", () => {
    for (const t of ["hardcharger://h", " https://h", "jdbc:hardcharger://h", "chargewire://h"])
      expect(looksLikeUri(t)).toBe(true);
    for (const t of ["h", "db.example.com", "::1", "10.0.0.1"]) expect(looksLikeUri(t)).toBe(false);
  });
});

describe("serverUrl and the field checks", () => {
  it("composes the relay's URL, bracketing IPv6", () => {
    expect(serverUrl({ host: "h", port: 31337, tls: true, prefix: "" })).toBe("https://h:31337");
    expect(serverUrl({ host: "::1", port: 80, tls: false, prefix: "/p" })).toBe("http://[::1]:80/p");
    expect(addressLabel({ host: "::1", port: 31337 })).toBe("[::1]:31337");
  });

  it("checks host, port and prefix", () => {
    expect(hostProblem("")).toBeDefined();
    expect(hostProblem("https://h")).toBeDefined();
    expect(hostProblem("db.example.com")).toBeUndefined();
    expect(portProblem(31337)).toBeUndefined();
    expect(portProblem(0)).toBeDefined();
    expect(portProblem(Number.NaN)).toBeDefined();
    expect(normalPrefix("/chargewire/")).toBe("");
    expect(normalPrefix("proxy/cw/")).toBe("/proxy/cw");
  });
});
