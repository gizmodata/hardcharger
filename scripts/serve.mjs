#!/usr/bin/env node
// Serves the built UI (dist/) at /ui/ the way hardcharger-server does (scripts/ui-contract.mjs)
// and passes /warp/… through to a Warp SQL server, so the page and the protocol share one
// origin, as in production. The e2e tests run against it; it also works by hand:
//
//   node scripts/serve.mjs --upstream http://127.0.0.1:31337 [--port 5180] [--dist dist]

import { createReadStream, existsSync, statSync } from "node:fs";
import http from "node:http";
import path from "node:path";
import { parseArgs } from "node:util";

import { cacheControl, contentTypes, resolveUiPath, securityHeaders } from "./ui-contract.mjs";

export function createUiServer({ dist, upstream }) {
  const root = path.resolve(dist);
  const target = new URL(upstream);
  const isFile = (rel) => {
    try {
      return statSync(path.join(root, rel)).isFile();
    } catch {
      return false;
    }
  };

  return http.createServer((req, res) => {
    const url = new URL(req.url ?? "/", "http://placeholder");
    if (url.pathname === "/warp" || url.pathname.startsWith("/warp/")) {
      const upstreamReq = http.request(
        {
          host: target.hostname,
          port: target.port,
          method: req.method,
          path: req.url,
          headers: { ...req.headers, host: target.host },
        },
        (upstreamRes) => {
          res.writeHead(upstreamRes.statusCode ?? 502, upstreamRes.headers);
          upstreamRes.pipe(res);
        },
      );
      upstreamReq.on("error", () => {
        if (!res.headersSent) res.writeHead(502, { "Content-Type": "text/plain" });
        res.end("upstream unreachable");
      });
      req.pipe(upstreamReq);
      res.on("close", () => upstreamReq.destroy());
      return;
    }
    if (url.pathname === "/ui") {
      res.writeHead(308, { Location: "/ui/", ...securityHeaders });
      res.end();
      return;
    }
    if (!url.pathname.startsWith("/ui/") || (req.method !== "GET" && req.method !== "HEAD")) {
      res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8", ...securityHeaders });
      res.end("not found\n");
      return;
    }
    let rest;
    try {
      rest = decodeURIComponent(url.pathname.slice("/ui/".length));
    } catch {
      rest = undefined;
    }
    const file = rest === undefined ? undefined : resolveUiPath(rest, isFile);
    if (file === undefined || !isFile(file)) {
      res.writeHead(404, {
        "Content-Type": "text/plain; charset=utf-8",
        "Cache-Control": "no-store",
        ...securityHeaders,
      });
      res.end("not found\n");
      return;
    }
    const full = path.join(root, file);
    res.writeHead(200, {
      "Content-Type": contentTypes[path.extname(file)] ?? "application/octet-stream",
      "Content-Length": statSync(full).size,
      "Cache-Control": cacheControl(file),
      ...securityHeaders,
    });
    if (req.method === "HEAD") res.end();
    else createReadStream(full).pipe(res);
  });
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const { values } = parseArgs({
    options: {
      dist: { type: "string", default: "dist" },
      port: { type: "string", default: "5180" },
      host: { type: "string", default: "127.0.0.1" },
      upstream: { type: "string", default: "http://127.0.0.1:31337" },
    },
  });
  if (!existsSync(path.join(values.dist, "index.html"))) {
    console.error(`no ${values.dist}/index.html: run npm run build first`);
    process.exit(1);
  }
  const server = createUiServer({ dist: values.dist, upstream: values.upstream });
  server.listen(Number(values.port), values.host, () => {
    console.log(`Hardcharger UI on http://${values.host}:${server.address().port}/ui/ (Warp SQL: ${values.upstream})`);
  });
}
