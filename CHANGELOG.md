# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- The web UI for Hardcharger, a static single-page app (React 19, Vite, TypeScript) that the
  server serves at `/ui` on its own origin and that speaks Warp SQL to it with
  `@gizmodata/warp-sql` (vendored from gizmodata/warp-sql-js at a pinned commit, with a
  provenance check that rebuilds it).
- Sign-in with Hardcharger's password flow; only the short-lived access token is kept, in
  memory and the tab's `sessionStorage`. An expired token, or a session an administrator ended,
  brings back the sign-in page with the reason.
- SQL editor (Monaco, trimmed to what SQL needs): Ctrl/⌘+Enter runs the statement under the
  cursor or the selection; tabs, each with its own server session; keyword and name completion.
- Results grid that draws only the rows in view (up to a 1,000,000-row limit), with DuckDB's
  formatting of every type; cell, row and column selection; Ctrl/⌘+C copies as tab-separated
  text in every browser, Safari included; a whole value in a dialog on double-click.
- Cancel that stops the statement on the server.
- Schema browser over the Warp SQL metadata calls (catalogs, schemas, tables, views, columns
  and their types), with table previews and name insertion.
- Exports: CSV, TSV, JSON with exact numbers, and Parquet (parquet-wasm).
- Sessions page (`hardcharger_sessions()`): administrators see and end (`KILL SESSION`) every
  session; other users see their own.
- About dialog with the server, protocol, capabilities, session and sign-in expiry.
- GizmoData look, light and dark.
- Recovery from a stale build: a missing lazily loaded file reloads the page after 0, 3 and
  10 seconds, then shows a "new version available" notice; error boundaries at the root and
  inside the layout.
- A strict Content-Security-Policy (no inline code, no `eval`; `'wasm-unsafe-eval'` for the
  Parquet export only) and the serving contract the server implements
  (`scripts/ui-contract.mjs`), with `scripts/serve.mjs` serving a build the same way.
- Reproducible release tarballs (`hardcharger-ui-<version>.tar.gz`) with `SHA256SUMS` and a
  content SHA-256 that server builds pin.
- Tests: unit tests (vitest), and Playwright e2e tests in Chromium and WebKit against a real
  `hardcharger-server` with access control on, failing on any console error, CSP violation or
  unexpected HTTP error. CI on every push; releases on `v*` tags, gated on the full CI.
