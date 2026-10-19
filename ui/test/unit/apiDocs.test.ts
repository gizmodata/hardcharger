import { afterEach, describe, expect, it, vi } from "vitest";

import {
  NoOpenApiError,
  authorizeRequests,
  downloadBinaryResponses,
  isSameOrigin,
  loadOpenApi,
  openApiUrl,
  sameOriginSpec,
} from "../../src/lib/apiDocs";

const origin = "https://db.example.com:8443";
const token = "tok-123";

describe("which requests carry the token", () => {
  const intercept = authorizeRequests(() => token, origin);

  it.each([
    `${origin}/warp/v1/health`,
    `${origin}/warp/v1/sessions/s_1/metadata/server-info?x=1`,
    "/warp/v1/sessions",
    "warp/v1/sessions",
  ])("adds Authorization for %s (this origin)", (url) => {
    expect(intercept({ url, headers: { Accept: "application/json" } }).headers).toEqual({
      Accept: "application/json",
      Authorization: `Bearer ${token}`,
    });
  });

  it.each([
    "https://evil.example.com/warp/v1/health",
    "https://db.example.com/warp/v1/health", // another port
    "http://db.example.com:8443/warp/v1/health", // another scheme
    "https://db.example.com:8444/warp",
    "//evil.example.com/warp",
    "https://db.example.com:8443@evil.example.com/warp",
    "https://db.example.com:8443.evil.example.com/warp",
    "javascript:alert(1)",
    "data:text/plain,hi",
    "http://[::1",
  ])("never adds it for %s, and drops one already there", (url) => {
    const out = intercept({ url, headers: { authorization: "Bearer someone-else", "X-A": "1" } });
    expect(out.headers).toEqual({ "X-A": "1" });
    expect(JSON.stringify(out)).not.toContain(token);
  });

  it("reads the token when the request is sent, and sends none without one", () => {
    const signIn: { token?: string } = {};
    const late = authorizeRequests(() => signIn.token, origin);
    expect(late({ url: `${origin}/warp` }).headers).toEqual({});
    signIn.token = "later";
    expect(late({ url: `${origin}/warp` }).headers).toEqual({ Authorization: "Bearer later" });
  });

  it("replaces an Authorization header of any case on this origin", () => {
    const out = intercept({ url: `${origin}/warp`, headers: { AUTHORIZATION: "Basic x" } });
    expect(out.headers).toEqual({ Authorization: `Bearer ${token}` });
  });
});

describe("isSameOrigin", () => {
  it("compares scheme, host and port", () => {
    expect(isSameOrigin("http://127.0.0.1:31337/warp", "http://127.0.0.1:31337")).toBe(true);
    expect(isSameOrigin("http://localhost:31337/warp", "http://127.0.0.1:31337")).toBe(false);
    expect(isSameOrigin("https://db.example.com:443/warp", "https://db.example.com")).toBe(true);
  });
});

describe("where the document is", () => {
  it("is nowhere unless discovery lists the openapi capability", () => {
    expect(openApiUrl({ capabilities: ["transactions"] }, origin)).toBeUndefined();
    expect(openApiUrl({}, origin)).toBeUndefined();
  });

  it("is links.openapi on this origin, or the default path", () => {
    expect(openApiUrl({ capabilities: ["openapi"] }, origin)).toBe(`${origin}/warp/v1/openapi.json`);
    expect(openApiUrl({ capabilities: ["openapi"], links: { openapi: "/db/warp/v1/openapi.json" } }, origin)).toBe(
      `${origin}/db/warp/v1/openapi.json`,
    );
    expect(
      openApiUrl({ capabilities: ["openapi"], links: { openapi: "https://evil.example.com/o.json" } }, origin),
    ).toBeUndefined();
  });
});

describe("loadOpenApi", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("refuses without fetching when discovery doesn't list it", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    await expect(loadOpenApi({ capabilities: [] }, origin)).rejects.toBeInstanceOf(NoOpenApiError);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("turns a 404 or a document that isn't OpenAPI into NoOpenApiError", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("nope", { status: 404 })));
    await expect(loadOpenApi({ capabilities: ["openapi"] }, origin)).rejects.toBeInstanceOf(NoOpenApiError);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ swagger: "2.0" })));
    await expect(loadOpenApi({ capabilities: ["openapi"] }, origin)).rejects.toBeInstanceOf(NoOpenApiError);
  });

  it("returns the document", async () => {
    const fetch = vi.fn().mockResolvedValue(Response.json({ openapi: "3.1.1", paths: {} }));
    vi.stubGlobal("fetch", fetch);
    await expect(loadOpenApi({ capabilities: ["openapi"] }, origin)).resolves.toEqual({ openapi: "3.1.1", paths: {} });
    expect(fetch.mock.calls[0]![0]).toBe(`${origin}/warp/v1/openapi.json`);
  });
});

describe("sameOriginSpec", () => {
  it("replaces every servers list with this origin, leaving the original alone", () => {
    const doc = {
      openapi: "3.1.1",
      servers: [{ url: "{origin}", variables: { origin: { default: "http://127.0.0.1:31337" } } }],
      paths: {
        "/warp": { servers: [{ url: "https://elsewhere" }], get: { servers: [{ url: "https://x" }], responses: {} } },
      },
    };
    const out = sameOriginSpec(doc, origin);
    expect(out.servers).toEqual([{ url: origin, description: "This server" }]);
    expect(JSON.stringify(out.paths)).not.toContain("servers");
    expect(doc.paths["/warp"].servers).toHaveLength(1);
  });
});

describe("binary answers", () => {
  it("offers Arrow streams as a download", () => {
    const out = downloadBinaryResponses({ headers: { "content-type": "application/vnd.apache.arrow.stream" } });
    expect(out.headers?.["content-disposition"]).toBe('attachment; filename="result.arrows"');
  });

  it("offers other bytes as a download too", () => {
    const out = downloadBinaryResponses({ headers: { "Content-Type": "application/octet-stream" } });
    expect(out.headers?.["content-disposition"]).toBe('attachment; filename="response.bin"');
  });

  it.each(["application/json", "application/problem+json", "text/plain; charset=utf-8", "application/yaml"])(
    "leaves %s to be shown",
    (type) => {
      expect(downloadBinaryResponses({ headers: { "content-type": type } }).headers).toEqual({ "content-type": type });
    },
  );

  it("keeps a Content-Disposition the server sent", () => {
    const headers = { "content-type": "application/vnd.apache.arrow.stream", "content-disposition": "inline" };
    expect(downloadBinaryResponses({ headers }).headers).toEqual(headers);
  });
});
