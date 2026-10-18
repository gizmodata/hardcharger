// How the web UI must be served: the contract hardcharger-server implements for /ui, and
// that scripts/serve.mjs (the e2e tests' server) implements the same way.

/**
 * The Content-Security-Policy for every /ui response. Everything the UI loads is a file of
 * its own under /ui/; the only exceptions are named here:
 *   - script-src 'wasm-unsafe-eval': Parquet export compiles WebAssembly (parquet-wasm).
 *     It allows WebAssembly only, not eval of JavaScript.
 *   - style-src 'unsafe-inline': the editor (Monaco) writes <style> elements and style
 *     attributes at run time.
 *   - img-src data:: two small images in the editor's stylesheet.
 */
export const contentSecurityPolicy = [
  "default-src 'self'",
  "script-src 'self' 'wasm-unsafe-eval'",
  "worker-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self'",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

/** Headers on every /ui response. */
export const securityHeaders = {
  "Content-Security-Policy": contentSecurityPolicy,
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "X-Frame-Options": "DENY",
  "Cross-Origin-Opener-Policy": "same-origin",
  "Cross-Origin-Resource-Policy": "same-origin",
};

/**
 * Hashed files (/ui/assets/…) never change; everything else is revalidated. A 404 under /ui
 * is never cached (Cache-Control: no-store): another replica or the next build may have it.
 */
export function cacheControl(path) {
  return path.startsWith("assets/") ? "public, max-age=31536000, immutable" : "no-cache";
}

export const contentTypes = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".wasm": "application/wasm",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".ttf": "font/ttf",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".txt": "text/plain; charset=utf-8",
};

/**
 * What a request for `rest` (the path after /ui/, decoded, no leading slash) gets:
 * a file, index.html (the single-page app's own routes), or nothing (404).
 *   - a file that exists: that file
 *   - under assets/: nothing (a missing hashed file is a stale page, not a route)
 *   - a last segment without a dot: index.html
 *   - anything else: nothing
 */
export function resolveUiPath(rest, exists) {
  if (rest === "" || rest === "index.html") return "index.html";
  if (rest.split("/").some((s) => s === ".." || s === ".")) return undefined;
  if (exists(rest)) return rest;
  if (rest.startsWith("assets/")) return undefined;
  const last = rest.split("/").pop() ?? "";
  return last.includes(".") ? undefined : "index.html";
}
