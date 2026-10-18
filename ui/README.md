<p align="center">
  <a href="https://gizmodata.com">
    <img src="../docs/assets/hardcharger-banner.svg" alt="Hardcharger by GizmoData" width="100%">
  </a>
</p>

# Hardcharger's web UI

The browser UI that [Hardcharger](../README.md#️-built-in-web-ui) serves at `/ui` when started
with `--web-ui`: a static single-page app (React 19, Vite, TypeScript) compiled into
`hardcharger-server`, on the server's own port, origin and TLS certificate. There is no Node
backend and no cross-origin access: the browser speaks [Warp SQL](https://github.com/gizmodata/warp-sql)
(plain HTTP and Apache Arrow) to the server with [`@gizmodata/warp-sql`](https://github.com/gizmodata/warp-sql-js).

<p align="center">
  <img src="../docs/assets/web-ui-query-light.png" alt="The web UI: the schema browser, the SQL editor and a result" width="80%">
</p>

- ✍️ **SQL editor** (Monaco): Ctrl/⌘+Enter runs the statement under the cursor, or the
  selection; several tabs, each with its own server session, so one tab's long query never
  holds up another's.
- 📊 **Results grid** for up to a million rows (only the rows in view are drawn), with DuckDB's
  own formatting of decimals, dates, timestamps, intervals, lists, structs, maps and blobs.
  Select cells, rows or columns and copy with Ctrl/⌘+C (Safari included), or copy everything.
- 🛑 **Cancel** stops the statement on the server, not just the wait for it.
- 🗂️ **Schema browser**: catalogs, schemas, tables, views and columns with their types, from the
  Warp SQL metadata calls, filtered by the server's access control; preview a table in a new
  tab; double-click to insert a name.
- 💾 **Exports**: CSV, TSV, JSON (exact numbers) and Parquet (every Arrow type, written in the
  browser by parquet-wasm).
- 👥 **Sessions**: an administrator sees every session on the server and can end one
  (`KILL SESSION`); everyone else sees their own.
- 🔐 **Hardcharger's own sign-in and access control.** The password goes once to
  `POST /warp/v1/auth/token`; the browser keeps only the short-lived access token, in memory
  and this tab's `sessionStorage`, never in `localStorage` or a cookie. Under mutual TLS, the
  browser presents its client certificate to load `/ui` at all, and with
  `--mtls-certificate-sign-in` the page offers **Sign in with my certificate** (the UI uses
  fetch's default credentials mode, never `omit`, which would leave the certificate off).
- 🎨 GizmoData's look: the README banner's orange and Warp blue, light and dark themes (the
  system's choice until you pick one), Inter embedded in the build (SIL Open Font License 1.1,
  served as `/ui/fonts/Inter-LICENSE.txt`).

## 🛡️ Serving it

`npm run build` writes a self-contained directory, `dist/`: `index.html`, `favicon.png`,
`fonts/Inter-LICENSE.txt`, and `assets/` with content-hashed file names. The server embeds it and serves it with these rules
(written down once in [`scripts/ui-contract.mjs`](scripts/ui-contract.mjs), which
`scripts/serve.mjs` and the e2e tests use):

| Request                                                             | Answer                                                          |
| ------------------------------------------------------------------- | --------------------------------------------------------------- |
| `/ui`                                                               | `308` to `/ui/`                                                 |
| `/ui/` and any path under `/ui/` whose last segment has no dot      | `index.html` (the app's own routes, such as `/ui/sessions`)     |
| an existing file                                                    | the file, with its content type (`.wasm` is `application/wasm`) |
| a missing file (anything under `/ui/assets/`, or a name with a dot) | `404`, `Cache-Control: no-store`                                |
| anything else outside `/ui` and `/warp`                             | not the UI's: the server's own `404`                            |

`/ui/assets/*` is `Cache-Control: public, max-age=31536000, immutable`; everything else
(`index.html` above all) is `no-cache`, so a new build is picked up at once. The server sends
a file gzip makes smaller as `Content-Encoding: gzip` (with `Vary: Accept-Encoding`) to a client
that accepts it and plain otherwise, each with its own `ETag`, and answers `If-None-Match` with
`304`. Every `/ui`
response carries `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
`X-Frame-Options: DENY`, `Cross-Origin-Opener-Policy: same-origin`,
`Cross-Origin-Resource-Policy: same-origin` and this Content-Security-Policy:

```text
default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self';
style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self';
object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'
```

The e2e tests run under this policy and fail on any violation; taking `'wasm-unsafe-eval'` or
`'unsafe-inline'` out makes them fail. The exceptions:

- `'wasm-unsafe-eval'`: the Parquet export compiles WebAssembly. It allows WebAssembly only,
  not `eval` of JavaScript.
- `style-src 'unsafe-inline'`: the editor writes `<style>` elements and style attributes at run
  time.
- `img-src data:`: two small images in the editor's stylesheet (the color picker's
  background; no test reaches it, and it costs nothing to allow).

The editor's worker is a file of its own (`worker-src 'self'`, no `blob:`).

When an upgrade replaces the hashed files, a tab opened before it may ask for one that is gone.
The page then reloads after 0, 3 and 10 seconds (long enough for a rolling restart), and after
that says that a new version is available. The header and the schema browser stay usable
throughout: the pages sit inside an error boundary of their own, inside the layout.

## 📦 How the server embeds it

`npm run build` writes `dist/`; `npm run package` packs it as
`release/hardcharger-ui-<version>.tar.gz` (the version is the server's, from
`server/CMakeLists.txt`): one directory, `hardcharger-ui/`, with the built files and `MANIFEST`
(a `<sha256>  <path>` line per file). The SHA-256 of `MANIFEST`, the _content SHA-256_, names
the build whatever the compression, and the build is reproducible: the same commit gives the
same content SHA-256 on any machine. `npm run package` also writes `SHA256SUMS` and
`hardcharger-ui-<version>.CONTENT_SHA256`.

CI builds the UI once per run, from this directory, and every server build in that run embeds
that tarball (`-DHARDCHARGER_WEB_UI=…`; [`server/cmake/web_ui.cmake`](../server/cmake/web_ui.cmake)
checks every file against `MANIFEST`, gzips each one and logs the content SHA-256). The server
answers a client that accepts gzip with the embedded bytes as they are, and inflates them for one
that doesn't. A release attaches the same tarball and its content SHA-256 next to the server's,
for serving the UI some other way (`scripts/serve.mjs` below).

## 🛠️ Developing

Node 24 or later.

```bash
npm ci
HARDCHARGER_UI_DEV_SERVER=http://127.0.0.1:31337 npm run dev   # http://localhost:5173/ui/, /warp proxied to the server
npm run lint        # eslint (typed rules, React hooks) and prettier
npm run typecheck   # the app, the unit tests, the e2e tests and the configs (a planted error fails it)
npm test            # unit tests (vitest)
npm run build       # dist/, then checks it: no inline code, hashed assets, nothing missing
npm run package     # release/hardcharger-ui-<version>.tar.gz, SHA256SUMS, its CONTENT_SHA256
```

The JavaScript client, `@gizmodata/warp-sql`, is not on npm. `vendor/` holds the package
`npm pack` makes from [gizmodata/warp-sql-js](https://github.com/gizmodata/warp-sql-js) at the
commit pinned in [`vendor/warp-sql-js.json`](vendor/warp-sql-js.json) (and
`package-lock.json` pins its hash again). `node scripts/vendor-warp-sql.mjs --verify --rebuild`
rebuilds that commit and checks every file; `--update <commit>` moves the pin.

### End-to-end tests

The e2e tests run the build in Chromium and WebKit against a real `hardcharger-server` with
access control on, which needs PostgreSQL. They start the server themselves, seed it (data, a
tenant and a non-admin user) and fail, never skip, without one:

```bash
docker run -d --name hardcharger-ui-test-pg -e POSTGRES_USER=hardcharger \
  -e POSTGRES_PASSWORD=hardcharger -e POSTGRES_DB=hardcharger \
  -p 127.0.0.1:55460:5432 postgres:17-alpine
export HARDCHARGER_UI_TEST_SERVER_BINARY=/path/to/hardcharger-server
# HARDCHARGER_UI_TEST_RBAC_STORE_URI=postgresql://… (default: that container)
npx playwright install chromium webkit
npm run build && npm run test:e2e
```

With `HARDCHARGER_UI_TEST_EMBEDDED=1` the tests run against the UI a server build embeds
(`hardcharger-server --web-ui`, built as in the [main README](../README.md#️-build-from-source))
instead of `dist/` served by `scripts/serve.mjs`, and include `*.embedded.spec.ts`: mutual TLS,
with its own server and certificates, which can't run through a proxy (the server binds tokens
to the certificate presented to it). CI runs them this way. The tests read only
`HARDCHARGER_UI_TEST_*` variables, and the server they start gets none of the shell's
`HARDCHARGER_*` settings.

Every page in every test fails the test on a console error, an uncaught exception, a CSP
violation or an HTTP answer of 400 or more, unless the test declares that error (and then the
error must happen). They cover sign-in (and the server's refusal of a wrong password), a
returning visitor's reload, running, errors, cancel, tabs, the row limit, the schema browser,
copying in both browsers (read back by pasting, the way a person would), every export (Parquet
read back), a stale build's recovery (a page module answered with 404), and a non-admin user who
sees only their tenant's catalog and is signed out when an administrator ends their sessions.

CI ([`server.yml`](../.github/workflows/server.yml)) runs the lint, typecheck and unit tests, the
client's provenance check, and the e2e tests in both browsers against the Linux x86-64 server
built in the same run, whenever `ui/`, the server's web UI code or the warp-sql submodule
changes, and always on `main` and on tags; a release waits for them. The provenance check
clones gizmodata/warp-sql-js with a read-only deploy key, the `DEPLOY_KEY_WARP_SQL_JS` secret.

## 📄 License

[Apache License 2.0](../LICENSE). Inter: [SIL Open Font License 1.1](public/fonts/Inter-LICENSE.txt).
