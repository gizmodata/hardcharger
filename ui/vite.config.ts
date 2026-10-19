import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

import { thirdPartyNotices } from "./scripts/third-party-notices.mjs";
import { uiVersion } from "./scripts/version.mjs";

const version = uiVersion();
// dist/third-party-notices.txt: every bundled package's licence (scripts/third-party-notices.mjs).
const notices = thirdPartyNotices({
  root: fileURLToPath(new URL(".", import.meta.url)),
  title: `Hardcharger web UI ${version}: third-party software notices`,
});
// The copyright year in About: the build's, from SOURCE_DATE_EPOCH when set (a reproducible build).
const buildYear = String(
  new Date(
    process.env["SOURCE_DATE_EPOCH"] ? Number(process.env["SOURCE_DATE_EPOCH"]) * 1000 : Date.now(),
  ).getUTCFullYear(),
);

// The UI is served by hardcharger-server at /ui on the same listener as Warp SQL, so every
// asset path starts with /ui/ and the app talks to /warp on its own origin.
//
// The build must work under the server's Content-Security-Policy (see README.md): no inline
// scripts, no eval, no data: URIs, workers from files of their own.
export default defineConfig({
  base: "/ui/",
  plugins: [react(), notices.plugin],
  resolve: {
    // Monaco's package exports map only its .js files; its CSS is reached by path.
    alias: [
      {
        find: /^monaco-editor-esm\/(.*)$/,
        replacement: fileURLToPath(new URL("node_modules/monaco-editor/esm/vs/$1", import.meta.url)),
      },
    ],
  },
  define: { __UI_VERSION__: JSON.stringify(version), __BUILD_YEAR__: JSON.stringify(buildYear) },
  build: {
    outDir: "dist",
    assetsDir: "assets",
    target: "es2023",
    sourcemap: false,
    // Everything as a file of its own: a data: URI would need img-src/font-src data:.
    assetsInlineLimit: 0,
    chunkSizeWarningLimit: 5000,
    // The polyfill would be inline code in no file of its own; every supported browser has
    // modulepreload.
    modulePreload: { polyfill: false },
  },
  worker: { format: "es", plugins: () => [notices.workerPlugin] },
  server: {
    // `npm run dev` against a local server: HARDCHARGER_UI_DEV_SERVER=http://127.0.0.1:31337
    proxy: { "/warp": { target: process.env["HARDCHARGER_UI_DEV_SERVER"] ?? "http://127.0.0.1:31337" } },
  },
});
