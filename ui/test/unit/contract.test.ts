import { describe, expect, it } from "vitest";

import { cacheControl, contentSecurityPolicy, resolveUiPath } from "../../scripts/ui-contract.mjs";

describe("serving /ui", () => {
  const files = new Set(["index.html", "favicon.png", "assets/index-abc12345.js"]);
  const exists = (p: string) => files.has(p);

  it("serves files, index.html for the app's routes, and nothing else", () => {
    expect(resolveUiPath("", exists)).toBe("index.html");
    expect(resolveUiPath("favicon.png", exists)).toBe("favicon.png");
    expect(resolveUiPath("assets/index-abc12345.js", exists)).toBe("assets/index-abc12345.js");
    expect(resolveUiPath("sessions", exists)).toBe("index.html");
    expect(resolveUiPath("a/deeper/route", exists)).toBe("index.html");
    expect(resolveUiPath("assets/gone-12345678.js", exists)).toBeUndefined();
    expect(resolveUiPath("assets/route", exists)).toBeUndefined();
    expect(resolveUiPath("missing.png", exists)).toBeUndefined();
    expect(resolveUiPath("../secret", exists)).toBeUndefined();
    expect(resolveUiPath("assets/../index.html", exists)).toBeUndefined();
  });

  it("caches hashed files forever and revalidates the rest", () => {
    expect(cacheControl("assets/index-abc12345.js")).toBe("public, max-age=31536000, immutable");
    expect(cacheControl("index.html")).toBe("no-cache");
  });

  it("allows nothing from elsewhere, and no script eval", () => {
    expect(contentSecurityPolicy).toContain("default-src 'self'");
    expect(contentSecurityPolicy).toContain("frame-ancestors 'none'");
    expect(contentSecurityPolicy).not.toMatch(/'unsafe-eval'|https?:|\*/);
  });
});
