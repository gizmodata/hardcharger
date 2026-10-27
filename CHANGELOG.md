# Changelog

All notable changes to the Hardcharger server are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The Python package keeps its
own changelog in `python/CHANGELOG.md`.

## [Unreleased]

## [2.1.1] - 2026-10-27

A patch release that fixes "Too many open files" on DuckLake tables with many partition files.

### Fixed

- **"Too many open files" reading a partitioned DuckLake table** ([#8](https://github.com/gizmodata/hardcharger/issues/8)). A scan of a DuckLake
  table holds many of its data files open at once, and a table with thousands of partition
  files went past the 1024-file soft limit that Docker, systemd and Kubernetes start
  processes with, so queries failed with `IO Error: Cannot open file ...: Too many open files`.
  The server now raises its soft open-file limit to the hard limit at startup (as Go programs
  and most servers do), logs `raised the open-file limit` with the old and new values, and
  warns when even the hard limit is below 8192. Windows has no such limit and is unchanged.

## [2.1.0] - 2026-10-26

Hardcharger 2.1 introduces a desktop app for macOS and Windows, SQL monitoring and
server configuration in the SQL workspace, and fixes for bulk ingest and VARIANT results.
The existing server and command-line packages remain available.

### Added

- **Hardcharger Desktop** for macOS 15+ on Apple silicon and Windows on x64 and Arm64.
  The app bundles the server and CLI with a dashboard and the SQL workspace. Manage
  the server from the menu bar or system tray, keep it running after closing the
  dashboard, and optionally launch it at login. macOS downloads are signed and
  notarized `.dmg` installers; Windows downloads are signed `.msi` installers.
- **Desktop server management:** searchable settings with appropriate boolean/enum
  controls, reviewed YAML imports and exports that omit secrets, live server logs,
  and confirmation before stopping or restarting the server.
- **Guided network connections:** opt-in network access, local IP/hostname detection,
  generated TLS certificates, explicit certificate-trust setup, OS-protected saved
  credentials, and copyable Web UI, Python/ADBC, JDBC, CLI and JavaScript connection
  examples using the server's real address and `hardcharger://` URIs.
- **SQL Monitor** in both the desktop workspace and the server's `--web-ui`, for
  unrestricted administrators when instrumentation is enabled. See queued, executing,
  fetching, succeeded, failed, cancelled and timed-out queries; filter recent history
  (10 minutes by default) while active work remains visible. View statement slots and
  queue capacity, cancel work, or end a session with confirmation.
- **Query details and captured profiles:** open details inline or in a separate window,
  follow live status and elapsed time for each phase, scroll and copy the SQL, and inspect
  captured DuckDB metrics, operator trees and profile JSON without rerunning the query.
- **Server Configuration** for unrestricted administrators: inspect session and global
  values, change or reset supported runtime settings at either scope, and inspect
  read-only startup settings. The Sessions tab also shows client IP addresses and
  identifies connections from this computer.

### Changed

- **Runtime extensions on non-iOS platforms:** httpfs, PostgreSQL, DuckLake, tpch and
  autocomplete are no longer statically included. Known extensions install and load
  automatically by default, matching GizmoSQL, and administrators can choose their
  versions and repositories. Extension-signature verification remains enabled. iOS
  retains its built-in extensions because dynamic loading is unavailable there.
- **DuckLake follows upstream metadata compatibility.** The project-specific metadata
  storage-version patch is removed. Check compatibility before using older readers
  after newer-server writes to an existing DuckLake catalog.
- **Python package versions match the server:** `hardcharger==2.1.0` installs server
  2.1.0. Subsequent Python-only fixes use `.postN` releases with the same server pin.

### Fixed

- **Bulk-ingest column ordering:** `append` and `create_append` into an existing table
  bind uploaded columns by name. Different column order no longer causes incorrect
  casts or silently swaps values between columns of the same type. Omitted columns
  use table defaults; unknown columns fail without writing rows. This server fix
  resolves [hardcharger-adbc#1](https://github.com/gizmodata/hardcharger-adbc/issues/1)
  without requiring a new ADBC driver release.
- **Readable VARIANT values** in the web UI and CLI, including scalars, objects, arrays
  and nulls ([#2](https://github.com/gizmodata/hardcharger/issues/2)). The web UI identifies
  the logical VARIANT type; copy and text exports preserve decoded values and exact
  numbers, while Arrow/Parquet downloads preserve typed data. Ordinary structs are
  still displayed as structs.
- **Oversized ingest allocations:** reject Arrow IPC messages and expanded compressed
  buffers before allocating beyond the configured limit. `max_ingest_message_bytes`
  defaults to 64 MiB per message and covers ordinary/partitioned ingest, dictionaries
  and Arrow prepared parameters. Oversized input returns HTTP 413 `payload_too_large`;
  larger uploads can be split into smaller record batches.
- **Timeout reporting:** instrumentation distinguishes statement and queue-wait timeouts
  from other failures, including the reason a queued statement timed out.

### Known limitations

- Slow readers and uploaders can still occupy shared engine workers; resumable transfer
  scheduling is tracked in [#4](https://github.com/gizmodata/hardcharger/issues/4).
- Server-wide retained and streaming-buffer budgets remain follow-up work in
  [#5](https://github.com/gizmodata/hardcharger/issues/5). The ingest-message limit is not
  a whole-server memory ceiling.

## [2.0.0] - 2026-10-20

The first general release of Hardcharger: a SQL server on DuckDB 2.0.0 that speaks Warp SQL
1.0.1 over plain HTTP, with access control (users, roles, grants, catalog access, scoped
tokens, sign-in through an identity provider or a client certificate), a web UI embedded in
the server (query editor, results, schema browser, uploads, sessions, API page), the
`hardcharger` command-line client, the Python package, Docker images, builds for Linux, macOS
and Windows, and the app for iPhone and iPad. This section lists what changed since
2.0.0-rc.1; the rc.1 section below has everything since 0.3.0.

### Added

- Docker: multi-architecture `:2.0.0-adbc` / `:latest-adbc` variants on GHCR and Docker
  Hub, with GizmoSQL's ten dbc-managed drivers and the native GizmoSQL, Oracle, Db2 and
  Quack drivers, plus the Hardcharger ADBC driver. System-wide manifests, stable library
  paths, version/digest inventories and notices are included. The separate `ADBC images`
  workflow checks every driver loads and queries Hardcharger by manifest on both native
  architectures before publishing the multi-architecture tags.
- Server: a startup banner in a text log: the "Hardcharger" wordmark (plain ASCII), the
  server's, Warp SQL's and DuckDB's versions, the web UI's address (or the Warp SQL endpoint's
  without `--web-ui`) and the documentation's, within 80 columns. Never in a JSON log.
  `--no-banner`, `--banner=false`, `HARDCHARGER_NO_BANNER=true`, `HARDCHARGER_BANNER=false` or
  `banner: false` leaves it out; every boolean option now takes `--no-NAME`. The Warp SQL
  version is the specification's (`info.version` of its OpenAPI document, read at configure
  time). Tests: `test_startup_log.py` (the art byte for byte against
  `tests/fixtures/banner-art.txt`, each way to turn it off, JSON), `startup_banner_test.cc`.
- Server: `log_emoji` (`--log-emoji`, `HARDCHARGER_LOG_EMOJI`): `auto` (the default) marks
  text log lines' levels with emoji only when the log goes to a terminal and the locale is
  UTF-8 (on Windows, a UTF-8 console code page), `always`, or `never`; JSON lines never have
  them. ✅ marks an outcome (ready, stopped), ⚠️ a warning, ❌ an error, 💥 the error that stops
  the server; info and debug lines get none. Tests at a pty (auto with a UTF-8 locale, with
  `C`, `never`, JSON) and in a file.
- Client: `--no-tls` connects without TLS (plain HTTP), as `--tls=false` does; `--tls false`
  (with a space) works too, as do `true`/`false`, `1`/`0`, `yes`/`no` and `on`/`off` after
  `--tls` and `--tls=`. Only those words are taken after a space; anything else stays the
  positional URI, and the error then names it (`--uri cannot be combined with --tls (the
  argument 'maybe' is read as the URI)`). `--help` starts with examples, the TLS-off one among
  them. In the shell, `.connect HOST PORT USERNAME [PASSWORD]` takes `--no-tls`, `--tls`,
  `--tls=BOOL` or `--tls BOOL` (a URI says it itself and refuses them), and `.help` and the
  "not connected" hint say so. Tests: `test_client_cli.py` (each spelling against a plain
  server, and each spelling of on as its twin).

- **Upload data** in the web UI: a CSV, TSV, Excel (.xlsx, .xls, .xlsb), Parquet, JSON lines /
  JSON array or Arrow IPC file into a new or existing table, from the query toolbar's **Upload
  data** button, the schema browser (its toolbar, and **Upload data…** on a catalog's or
  schema's menu), or a file dropped anywhere on the page. The dialog follows GizmoData Cloud's
  SQL editor (the same fields, labels, modes, table-name derivation and result message): File,
  Catalog, Schema (or a new one), Table, **If the table…** (create, which fails if it exists;
  append, with the file's columns matched to the table's by name and NULL for the ones it
  lacks; replace, the table's columns and rows), Header row, and the CSV delimiter or the Excel
  worksheet (a picker of the workbook's sheets). **Preview & column types**, folded away until
  opened, shows the first 100 rows as they will load, with each column's inferred type to
  change (VARCHAR, BIGINT, DOUBLE, DECIMAL(p,s), BOOLEAN, DATE, TIMESTAMP, TIMESTAMPTZ, TIME),
  rename or skip; a value that wouldn't fit shows in red. An append warns where the file's
  types differ from the table's, and stops before it starts when the file has a column the
  table hasn't. The file is read and converted to Arrow in the browser, in a Web Worker (a
  large or malformed file can't freeze the page), and sent over Warp SQL's bulk ingest in
  pieces of about 8 MB, one request each, in one transaction of the upload's own session (as
  the tab's **Run as** role): the server never stores the raw file, a progress bar counts
  rows and bytes, **Cancel upload** rolls everything back, and a failed upload leaves the
  table as it was. CSV, JSON lines, Parquet (row group by row group, with parquet-wasm) and
  Arrow streams are read as they upload, at any size; Excel workbooks and JSON arrays are read
  whole, up to 100 MB. CSV: the delimiter (`,` `;` tab `|`, or one typed, up to 4 characters),
  quotes, line breaks inside quotes and the header row are detected; UTF-8, UTF-16 with a byte
  order mark, and otherwise Windows-1252 with a note. A whole number with a leading zero
  (`00123`) stays text. Excel: dates and times from the cells' number formats and the
  workbook's own date system (1900 or 1904), a formula's saved value (none is calculated),
  numbers, booleans, text (a "number stored as text" stays text), error cells as NULL, empty
  rows left out and merged ranges' values in their first cell, each said in a note. A value
  that doesn't convert fails the upload with its line or cell and column (`Cell C12, column
  "amount": "n/a" is not a whole number`); the server's permission errors (CREATE, INSERT,
  row policies) are shown as it words them. The result says the rows, mode, format and time,
  lists the table's columns, refreshes the schema browser and autocomplete, and **Query this
  table** opens a tab with `SELECT * … LIMIT 100`. Excel is read with SheetJS Community
  Edition 0.20.3 (Apache-2.0), vendored from SheetJS's own CDN at a pinned SHA-256
  (`ui/vendor/sheetjs.json`, checked by `npm run vendor:verify`), not npm's abandoned `xlsx`
  0.18.5 (CVE-2023-30533, CVE-2024-22363): it reads .xlsx, .xls and .xlsb without running
  macros or formulas and needs no change to the UI's Content-Security-Policy (no `eval`, no
  workers of its own, nothing fetched). It and the converters load only when a file is read,
  in the upload worker (SheetJS about 490 KB, the worker 230 KB, the dialog 33 KB; the main
  bundle grows by about 3 KB).

- `default_catalog` and `default_schema`: where every new session starts, unless the client's
  own `catalog` / `schema` session options say otherwise. With neither set, an init SQL that
  ends in `USE x.y` sets it, as in GizmoSQL 1.x (on rc.1 sessions ignored it and started in
  the database's catalog), and the server logs that it did. Both are checked after the init
  SQL: a catalog that isn't attached, a missing schema or one of the server's own catalogs
  stops the server (exit 1). Under access control a session moves there only if its user's own
  `USE` could; otherwise it starts where it would have, and a tenant's default catalog wins.
  `hardcharger_server_info()` shows the administrator the effective defaults.
- `rbac_store_timeout_seconds` (default 10): every access-control store round trip is
  bounded. With PostgreSQL paused, a `CREATE TABLE` waited 152 s for the store; it now fails
  with `access control store unavailable: timed out after 10 s`, and what it was doing is
  rolled back once the store answers. PostgreSQL connection strings get libpq's
  `connect_timeout`, TCP keepalives, `tcp_user_timeout` and a `statement_timeout`, each
  unless they set it.
- Store outages are visible: a warning when the store stops answering (then every 30 s,
  with the snapshot's age), a line when it answers again, and the metrics
  `hardcharger_rbac_store_up` and `hardcharger_rbac_snapshot_age_seconds`.
  `rbac_max_snapshot_age_seconds` (default 0, no limit) refuses every principal but the
  administrator once the store has not answered for longer (fail closed).
- The access-control store belongs to one cluster: it records the first `cluster_id` that
  opens it and refuses a server of another cluster (`the access control store belongs to
  cluster ...`); `rbac_store_shared: true` shares users, roles and grants between clusters
  on purpose. The store's schema version is checked too: a store a later version upgraded
  is refused.

- The engine, host and process gauges GizmoData Cloud's Engine Metrics page reads from
  GizmoSQL 1.x, under `hardcharger_*` names with 1.x's meanings and units, in `/metrics` and
  `hardcharger_metrics()`: `hardcharger_duckdb_threads`, `hardcharger_duckdb_temp_storage_bytes`
  (from `duckdb_memory()`), `hardcharger_duckdb_spill_files` and `hardcharger_duckdb_spill_bytes`
  (live files in `duckdb_temporary_files()`), `hardcharger_database_file_bytes` and
  `hardcharger_wal_bytes` (the main database file DuckDB opened and its `.wal`; attached catalogs
  don't count), `hardcharger_health_check_duration_seconds`,
  `hardcharger_tls_certificate_expiry_seconds` (the serving certificate's notAfter as Unix time,
  read again whenever a client CRL reload rebuilds the TLS context),
  `hardcharger_metrics_collection_duration_seconds`, `hardcharger_host_memory_bytes`,
  `hardcharger_host_cpus`, `hardcharger_cgroup_memory_limit_bytes` and
  `hardcharger_cgroup_cpu_quota_cores` (cgroup v2, else v1, read on every collection so an
  in-place pod resize shows), and the standard `process_start_time_seconds`, `process_open_fds`
  and `process_threads` (Linux, macOS and Windows; Windows has no `process_open_fds`). As in 1.x,
  a measurement that can't be taken is left out rather than reported as 0: no file sizes for an
  in-memory or remote database, no certificate expiry without TLS, no cgroup gauges without a
  limit.

- **`trusted_proxies`** (`--trusted-proxies`, `HARDCHARGER_TRUSTED_PROXIES`): CIDRs or single
  addresses (IPv4 and IPv6) of the reverse proxies whose `X-Forwarded-For` or RFC 7239
  `Forwarded: for=` header names the client. From them, the client's address in the log, the
  audit records, `hardcharger_active_connections()` and the per-address bound on browser
  sign-ins is the rightmost forwarded hop that is not itself a trusted proxy (all trusted: the
  leftmost); `X-Forwarded-For` wins over `Forwarded`, and an unknown, obfuscated or malformed
  hop leaves the proxy's address. Behind an ingress these all showed the ingress's address
  before. The default (empty) trusts no one, and another peer's headers are ignored. A bad CIDR stops the server at startup (exit 2) naming it. The forwarded address
  is never used for authentication or TLS: client certificates and certificate-bound tokens
  stay tied to the connection. `hardcharger_active_connections()` has a new `proxy_address`
  column (the proxy's own `address:port` when the address was forwarded), and security log
  and audit lines a `proxy` field. Warp SQL C++ SDK `ServerOptions::trusted_proxies`. Tests:
  `test_trusted_proxies.py`, the SDK's `forwarded_test.cc`.

- **Users from the identity provider are created on first sign-in**, when asked:
  `token_auto_provision_users` (off by default; `token_auto_provision_issuer`, default
  `token_allowed_issuer`). Under access control, a verified sign-in token from that issuer
  (not an administrator's) whose user the store lacks creates the user, with no password and
  no grants, and keeps the token's role in its `provisioned_role` attribute: a user
  provisioned from a `readonly` token signs in readonly from then on until an administrator
  removes the attribute. Existing users are never changed, so a disabled user stays disabled.
  First sign-ins of one user on several replicas at once all succeed, and each creation is an
  access-control audit record with the user, the issuer and the role. Startup refuses the
  option without `rbac_store_uri`, without a way to verify tokens, or with an issuer other
  than `token_allowed_issuer`. Off, nothing changes: such a sign-in is refused, saying an
  administrator creates the user.

- One `info` log line for each cancelled statement, `statement cancelled`, with its
  `session_id`, `statement_id` (`q_…`), `user`, `reason` (Warp SQL's `cancel_reason`:
  `client`, `timeout`, `killed`, `cancelled`, `session_closed`, `server_shutdown`) and
  `duration` in seconds, whatever `print_queries` and `query_log_level` say (GizmoSQL 1.x
  logged `SQL Statement was successfully canceled.`). A statement that ends on its own or
  fails logs none. Tests: `test_statement_cancel_log.py`.

- **Hardcharger™ for iPhone and iPad** (`ios/`, iOS 18 and later): the Hardcharger server
  running on the device itself, its web UI and a native SQL console, and a client for
  Hardcharger servers anywhere. The server is `hardcharger-server` itself, built into the
  app as a library (`HARDCHARGER_EMBEDDED`, `server/embedded/hardcharger_embedded.h`) and
  cross-compiled for iOS devices and the simulator with the same recipes, pins and patches
  as every other platform (`ios/scripts/build-server-ios.sh`). The web UI opens signed in
  (an administrator password and token key made on first launch, kept in the Keychain); the
  console is the `hardcharger` client's own statement splitting, dot commands and output
  formatting. Import CSV, TSV, JSON and Parquet from Files, the share sheet or a drop;
  **Share on Local Network** (off by default) serves the database and web UI to computers on
  the same Wi-Fi over TLS with a certificate the device makes, shown with a QR code and its
  fingerprint; saved remote servers sign in with a password or the server's single sign-on.
  Sidebar, keyboard shortcuts and multiple windows on iPad. See `ios/README.md`.
- Web UI: on a touch-first device (a phone, a tablet without a mouse) the editor no longer
  takes the focus by itself when a tab shows, which brought up the keyboard over the results
  at the next tap; a tap on the editor still focuses it.
- The `hardcharger` client reads dollar-quoted strings (`$$ … $$`, `$tag$ … $tag$`): a `;`
  inside a function body no longer ends the statement.

### Changed

- An IPv6 client's address is written `[2001:db8::7]:51234` (it was `2001:db8::7:51234`,
  which can't be told apart from an address without a port), in
  `hardcharger_active_connections().client_address` and security events.

- **The idle rule is documented exactly** (README, `--help` for `session_idle_timeout`): a
  session is evicted within 5 s after `session_idle_timeout` seconds without a request, unless
  a statement of it is running, an ingest is in progress, a result is being read, or it holds a
  retained result that hasn't expired. That last one is Warp SQL §5.7's rule, kept: a session
  whose statement ran in JSON mode lives up to `result_retention_seconds` after it ended. Set
  `result_retention_seconds` no higher than `session_idle_timeout` to bound sessions by the idle
  timeout alone.

- CI (`server.yml`): an `ios` job (the app built for the simulator, its smoke test inside a
  simulator, unit and UI tests) on `v*` tags and on manual runs with `ios: true` only; on
  `v*` tags, `ios-archive` builds the signed App Store `.ipa` as the run's artifact and
  uploads it to TestFlight (App Store Connect API key; the build number is the run number).
- The server's log file is closed when logging is configured again (a server started again
  in the same process, as in the app).

### Fixed

- Web UI: update the embedded Warp SQL client so queries finishing during sign-out do not
  send statement-cleanup requests after their session has ended. Connection close releases
  those statements, avoiding spurious 404 errors while background queries finish.

- Web UI: pasted SQL reaches the editor in Linux WebKit as well as Chromium and Safari.
  The SQL editor uses the plain-text clipboard path instead of optional Paste As providers,
  keeping the pasted text and its indentation intact.
- Release CI now gates container publishing and the GitHub Release on the iOS simulator
  tests, signed device build and TestFlight upload. A manual run with `ios` and `sign`
  proves the signed device build before tagging, without uploading it. Trusted-proxy
  parsing also refuses abbreviated IPv4 addresses and ports on Windows consistently
  with Unix (Warp SQL SDK update).
- Server: a text log line leaves out a field with no value (`health check passing` printed
  an empty `error=`); JSON lines keep every key, and the passing health check's has no
  `error` at all. A health check that fails from the start is now logged too (only changes
  were). Tests: `test_startup_log.py`.
- Server: the startup log printed `url=http://0.0.0.0:31337/ui/`, which a browser can't
  open. On a wildcard bind (`0.0.0.0`, `::`) the `Hardcharger is ready` and `the web UI is
  on` lines (and the banner) now give `http://localhost:<port>` as `url` and
  `http://<host name>:<port>` as `also`; with TLS, the certificate's first DNS name that
  isn't a wildcard (else the host name), and localhost too when the certificate names it.
  `listen=` keeps the bind address. Unit tests for each case; `test_startup_log.py` for the
  log text.
- Client: a connection that fails says why. `--tls=false --port 9 -c "SELECT 1"` said only
  "not connected to a server" (a port or TLS on the command line didn't count as asking for a
  connection, so none was tried); it now connects and says `Could not connect to
  http://localhost:9: connection refused. Is the server running on that port?`. A script run
  without any connection options connects to the default at its first statement that needs
  the server (a script that only runs `.connect` or `.help` still needs none). Plain HTTP to a
  TLS server says to drop `--no-tls`; TLS to a plain-HTTP server names `--no-tls` (and
  `--tls=false`, `?tls=false`, `.connect … --no-tls`).
- Web UI: the editor's suggestion list showed empty rows (only the kind icons, no names) in
  every browser. The app's stylesheet had a rule for a class named `main` (the page's main
  area, `flex-direction: column`), and Monaco's suggestion rows have an element of that class
  too, so each row's name and detail were laid out below its icon, outside the 20 px row, and
  clipped. The app's generic class names that Monaco also uses (`main`, `tree`, `messages`,
  `message`) are renamed (`app-main`, `side-tree`, `run-messages`, `run-message`), and a unit
  test (`css-isolation.test.ts`) fails when a global rule names any class Monaco's stylesheets
  use, unless the rule is a deliberate Monaco override. The e2e test types a table's and a
  function's prefix and checks that each suggestion's label is drawn inside its row, is what
  the browser shows at its place, and has a colour other than its background, in Chromium and
  WebKit, before and after a visit to the API page. (The old tests checked only that rows
  were there and held the text.)
- Web UI: the API page's Swagger UI, with its stylesheet, now lives in a shadow root, so its
  rules (for generic names such as `.main`, `.info`, `.btn`, `.tab`) can't reach the rest of
  the app, and the app's (its `.btn`) don't reach Swagger UI. Before, Swagger UI's stylesheet
  stayed in the page for good after the first visit to the API page. Its dark theme follows
  the shadow host instead of a class on `<html>`. e2e: no rule of Swagger UI's in the page's
  stylesheets, and Swagger UI's buttons don't take the app's button style.
- Web UI: fast typing in the editor lost keys in Chromium (and moved the cursor to the start).
  The editor's own text came back to it as its value after React rendered, and when a key was
  pressed between that render and its effect, the older text was put back. The editor now
  tells its own text coming back from a value set from outside (opening a saved query, Preview
  data). e2e: a 93-character statement typed at script speed arrives whole (it lost keys in
  every Chromium run before).
- README: the web UI screenshots (the query view and the API page, light and dark) are
  retaken with the new logo and the API page in the header (`ui/test/e2e/screenshots.spec.ts`).
- Web UI: in a narrow side panel, a column's type now gives up its width before the column's
  name (`l_orderkey  BIG…`, not `l_or…  BIGINT NOT NULL`); a name is shortened only once its
  type is down to a few letters, and the row's tooltip has the whole name and type. A long
  type no longer runs under the row's **…** button, and the button keeps clear of a scroll bar
  drawn over the content (macOS). e2e in Chromium and WebKit at a narrow, scrolling panel.
- Web UI, **About**: capabilities are chips that wrap between names, never inside one
  (`ipc_compression_` / `zstd`); values wrap at spaces, and only one with none (an id) breaks
  inside a word. Each fact is listed once: the summary, then only the server-info keys it
  doesn't show (no second `server_name`, `server_version` or instance id), under a collapsed
  **All server info**. The dialog fits the window: the logo and title stay at the top and the
  notices at the bottom, and the details scroll between them (it was taller than a 900 px
  window). e2e in Chromium and WebKit at 1280×800 and phone width, light and dark.
- Web UI: SQL typed into the editor line by line was indented once more on every line (Enter
  kept the line's indentation, and the text's own spaces went on top). Enter still keeps the
  line's indentation (`autoIndent: "keep"`, no language rules), but indentation typed or
  pasted right after it replaces it; pasted text goes in as it is (no format or re-indent on
  paste). Enter after a word typed out in full is a new line, not that word's suggestion taken
  again (`acceptSuggestionOnEnter: "smart"`; WebKit lost the line break). e2e in Chromium and
  WebKit: a 4-line indented statement typed and pasted arrives byte for byte.
- Web UI: closing, reloading or leaving the page left its Warp SQL sessions open on the server
  until their idle timeout. It now ends them on `pagehide` (and on `beforeunload`, which some
  browsers send alone) with keepalive `DELETE`s carrying the token; with a transaction open the
  browser asks first, and the sessions stay if the user does. A page brought back from the
  back/forward cache loads again, with new sessions. e2e in Chromium and WebKit: the server
  lists none of the page's sessions within seconds of a close or a navigation, Back works, and
  staying at the transaction prompt keeps them.
- README: the `curl` example printed the statement's JSON metadata (`jq .result`), not the
  answer. It now asks for the Arrow stream and reads it with pyarrow (`[{'answer': 42}]`, run
  against a server), and says what the JSON document holds.
- Docs: benchmark results follow the TPC's fair-use policy. The runs are described as derived
  from TPC-H ("the 22 queries derived from TPC-H, SF10", "TPC-H-derived data"), never as TPC-H
  results; README's Performance section, `docs/benchmarks.md` (which also lists how the runs
  deviate from the specification), this changelog's benchmark entry and `bench/tpch.py` carry
  the disclaimer once: derived from the TPC-H benchmark, not an official TPC-H result and not
  comparable to published TPC-H results, and TPC-H™ a trademark of the Transaction Processing
  Performance Council (TPC).

- The web UI's JavaScript client (`ui/vendor`, gizmodata/warp-sql-js) moves to cb563f9, which
  fixes two things the first browser upload found: bulk ingest (and Arrow parameters) failed in
  any bundled app with `"throughDOM" not available in this environment` (apache-arrow installs
  that only as a side effect its `"sideEffects": false` lets bundlers drop), and `close()` could
  end a session before a statement's delete reached it, which the browser logged as a 404.

- `LOAD iceberg` without the avro extension installed now says first what to run
  (`INSTALL avro;`, then `LOAD iceberg` again); iceberg loads avro itself once it is
  installed. The server never installs or loads extensions by itself, so DuckDB's own
  reason was buried in a long initialization error. The same hint is given when init SQL
  fails this way.

- A DuckLake with a DuckDB metadata file stays readable by the DuckDB release that created
  it. The bundled DuckLake extension attached an existing metadata file with
  `STORAGE_VERSION 'latest'`, so the first write from Hardcharger upgraded the file, and
  DuckDB 1.5 then refused it (`cannot initialize database with storage version 7 - which is
  lower than what the database itself uses`). Our build of the extension now attaches an
  existing file without a storage version, so it keeps its own; `META_STORAGE_VERSION
  'latest'` in the `ATTACH` still upgrades it on purpose (it used to fail with a duplicate
  option). A new metadata file gets `--storage-version` when that is set, and the latest
  format otherwise, as before. `--storage-version` is now also the format of new files a
  plain `ATTACH` creates, not just of the `--database` file. The patch is Hardcharger's own
  (`server/scripts/extension-patches/ducklake/`), applied by the DuckDB build after DuckDB's
  patches for the extension (`server/scripts/patch-duckdb-extensions.cmake`), and the build
  fails if it didn't reach the extension's source. PostgreSQL metadata was never affected.

- **A readonly session can't leave readonly through a scoped token.** Under access control, a
  session signed in with a `readonly` token could run `CREATE SCOPED TOKEN`, and the token it
  got signs in as a user, so it could write with the user's grants. A readonly session now runs
  no access-control statement but `SHOW …` and `SET ROLE` (`permission_denied`, "has a readonly
  session and cannot run CREATE SCOPED TOKEN").
- **A slow download keeps its session.** The idle sweep evicted a session whose statement had
  finished while the client was still reading its unretained result (up to 32 MB of it is
  sent after the statement finishes), cutting the stream mid-body; a reader slower than
  `session_idle_timeout` plus 5 s for that tail lost it. A read in progress now keeps the
  session (the Warp SQL SDK; spec §5.7 says a request in progress counts as one).

- A `readonly` session could write through a data-modifying CTE
  (`WITH c AS (INSERT … RETURNING *) SELECT * FROM c`, also `UPDATE` / `DELETE`), which
  DuckDB reports as a `SELECT`. DuckDB's own analysis of every statement a readonly session
  prepares now decides, as in GizmoSQL 1.x: one that modifies any database (temp included)
  or writes a file is refused before it runs, also inside a transaction, in a batch, under
  `EXPLAIN ANALYZE` or as a prepared statement.
- A `readonly` session can change its own session again, as in GizmoSQL 1.x: `BEGIN` /
  `START TRANSACTION`, `COMMIT` / `END`, `ROLLBACK` / `ABORT`, `SET VARIABLE` /
  `RESET VARIABLE`, `USE`, `SET` / `RESET` of a setting DuckDB keeps for the session
  (`SET SESSION …`, or a plain `SET` of a session-scoped setting such as `TimeZone`), and the
  `PRAGMA`s that read (`table_info`, `database_list`, `version`, ...). They were refused as
  modifying state. `SET GLOBAL`, a plain `SET` of a setting whose default scope is global,
  settings that reach the server (resources, extensions, secrets, files, the profiler, the
  grammar), temporary tables, views and macros, and every other write stay refused. README:
  "Read-only sessions". Tests: `test_readonly_session.py`.

### Security

- A session can no longer be used by a weaker token of the same user. Sessions (and their
  statements, retained results, prepared statements and ingests) were bound to the user
  name only, while a session's DuckDB connection is bound to the token that opened it. So
  any token of that name could run statements through a stronger token's session, with
  that token's privileges, by naming its id (as the ADBC driver's session attach does): a
  readonly token through its user's session, a scoped token (`CREATE SCOPED TOKEN`)
  through the session of the user who minted it, a token with `catalog_access` rules
  through an unrestricted one's, an identity-provider token with other groups (and so
  other mapped roles). Every request now also compares the token's authority, a SHA-256
  of its role, scoped token, `catalog_access` rules, identity-provider issuer and groups,
  and its store user and tenant (`AuthorityKey`, `server/src/auth.hpp`): any difference
  gets `404 session_not_found` on every route, as another user's token would. A new
  token of the same authority (signing in again, also after a password change, or
  exchanging the same scoped token again) still reaches the session; the token's id,
  times, password fingerprint and sign-in method are not part of it, and the certificate
  binding is checked as before. A store user dropped and created again under the same
  name no longer reaches the old user's sessions either. Warp SQL SDK
  `Principal::authority_key` (warp-sql §9.2). Tests:
  `server/tests/test_session_authority.py`, `server/unit/authority_key_test.cc`.
- Access-control store errors no longer carry the store's connection string. While a
  PostgreSQL store was unreachable, `CREATE USER`, `GRANT`, `REVOKE` and every other
  access-control write answered the client, and logged, DuckDB's "Unable to connect to
  Postgres at ..." with the full `postgresql://user:password@...` string. Every store error
  is now redacted (the string as given and as the server rewrites it, a URL's
  `user:password@`, libpq's `password=` and URL parameters such as `password=` and
  `sslpassword=`), and so is a failing init SQL `ATTACH`, which stopped startup with the
  password in its log line. One helper (`server/src/redact.cc`) does it for the store, init
  SQL, the logs and the last-resort fatal line that stops the server (`main.cc`), which may
  quote a database path or an `ATTACH` string. Test: `test_startup_log.py` (a `password=`
  in a database path that DuckDB's open error quotes back).
- DDL no longer runs when its owner can't be recorded. During a store outage an
  administrator's (or a user's) `CREATE TABLE` / `CREATE VIEW` succeeded while its
  ownership record was silently dropped, leaving an object with no owner (and any stale
  record of an earlier object of that name in force). The owner is now recorded before the
  statement runs; when the store can't take it, the statement fails with the store's error
  and creates nothing, and a statement that fails after its owner was recorded puts the
  previous record back.

## [2.0.0-rc.1] - 2026-10-20

### Added

- Third-party notices in everything that ships. `THIRD_PARTY_NOTICES.md` lists the 49
  third-party components the binaries link statically, each with its version, licence, the
  binaries and platforms it is in, and its full licence and NOTICE texts: DuckDB, the 31
  libraries and data sets it vendors (Mbed TLS, RE2, Thrift, the Unicode data, jemalloc on
  Linux, the TPC-H generator, ...), the httpfs, DuckLake and PostgreSQL extensions, Arrow and
  what it bundles (zstd, LZ4, uriparser, xsimd), Boost, OpenSSL, jwt-cpp, yaml-cpp, zlib,
  replxx, CRoaring, libpq and libcurl. It is generated
  (`server/scripts/third-party-notices.py`) from `server/third-party-notices/`: each
  component's reviewed version and its texts, fetched from the source at that version. CI
  (`--check`, in every build job) fails when a recipe pins another version than the one the
  notices were made for, when the build declares a dependency (FetchContent, a DuckDB
  extension, an extension library, a vcpkg package) no component covers, or when the file is
  stale. The web UI's build writes `third-party-notices.txt` (served at
  `/ui/third-party-notices.txt`, linked from **About** as **Open-source licences**) from the
  packages actually in the bundle, lazy chunks and the editor's worker included (15 today:
  React, Monaco, Swagger UI, Apache Arrow, parquet-wasm, ...), with every licence and notice
  file each ships (Monaco's `ThirdPartyNotices.txt`, Swagger UI's NOTICE and its bundle's
  `swagger-ui-es-bundle.js.LICENSE.txt`), and fails when a bundled package declares no
  licence, has no licence file, or has a licence outside the allow-list (MIT, ISC, BSD-2/3,
  Apache-2.0, 0BSD, BlueOak, CC0, Unlicense, Python-2.0, MPL-2.0; Inter's OFL-1.1 by name).
  The tarballs, the Windows zips (whose `THIRD_PARTY_NOTICES.md` ends with the versions and
  copyright files of the libraries the Windows build links from vcpkg), the MSIs (installed
  under Program Files\Hardcharger) and the container image (`/opt/hardcharger/` and
  `/usr/share/doc/hardcharger/`) carry `THIRD_PARTY_NOTICES.md` and
  `THIRD_PARTY_NOTICES-web-ui.txt`, and `hardcharger-server --licenses` and
  `hardcharger --licenses` print them from the binary itself (the server's followed by its
  embedded UI's). Tests: `test_server_cli.py`, `test_client_cli.py`, the unit test of the UI
  file reader, `third-party-notices.test.ts` (the allow-list, module-to-package mapping, the
  file's contents and its failures), `about.spec.ts` (Chromium and WebKit), the tarball smoke
  test and the image smoke test.
- Web UI, **API** page (`/ui/api`, next to Query and Sessions): the server's Warp SQL API in
  Swagger UI (swagger-ui-dist 5.33.3, in the build, loaded only when the page is opened:
  +1.7 MB, +435 KiB gzipped in the binary), from the OpenAPI 3.1 document the server now serves
  at `GET /warp/v1/openapi.json` (no sign-in; discovery lists capability `openapi` and
  `links.openapi`). Every request goes to the server's own origin (no server switcher, no
  Authorize dialog); **Try it out** carries the signed-in user's token, added to each request
  to this origin as it leaves and never to another origin, stored, or shown in the Curl box.
  Arrow answers are offered as a download (`result.arrows`); the dark theme applies; a server
  without the document gets a note. No change to the Content-Security-Policy. e2e:
  `api.spec.ts` in Chromium and WebKit; unit: `apiDocs.test.ts`.
- Web UI, the rest of the editor's behavior: **How the editor works** (the ? in the tab bar):
  sessions, with this server's idle timeout (the session document's `idle_timeout_s`),
  transactions, statements, results, and what stays in this browser; the Saved queries panel
  says they're kept in this browser. On a screen 760 px wide or less the side panel sits above
  the editor at full width (it was hidden) and the page scrolls, with a note, hidden for good
  by its ×, that a larger screen is easier. After a save, the cursor goes back to the editor
  (it was left on the page, so the next shortcut went nowhere). e2e: `idle.spec.ts` against a
  second server whose sessions end after 2 s idle (a new session takes over and says so; with
  a transaction open the run is refused and nothing of it executes; the schema browser carries
  on), `layout.spec.ts`, and every editor shortcut (Run, Run all, Save, close) acting on the
  shown tab rather than the tab opened last (`tabs.spec.ts`).
- Web UI: the README's screenshots show the editor's tabs, a script's result tabs and an open
  transaction; tests for a script's 64 MiB cap (unit) and for a browser that blocks IndexedDB
  (`saved-blocked.spec.ts`: the panel says it can't keep saved queries, Save says so, queries
  still run).
- Web UI, saved queries, Run as and downloads: saved queries in folders, kept in this
  browser (IndexedDB, per user; the page works without it), with Save (Ctrl/⌘+S; the same
  query after the first time), Save as…, the unsaved dot, new folder, rename, move by drag,
  delete (asking, with counts), import .sql files, a query's .sql or a folder's .zip, opening
  a query (or going to its tab), and the whole library exported to a file and imported in
  another browser. Run as: under access control a user's tab acts with one of their roles
  (SET ROLE) or all of them, the schema browser and autocomplete listing what that role sees;
  locked in a transaction; no picker for an administrator or without access control.
  Downloads: CSV, TSV, JSON and Excel (.xlsx, written here) of the rows as shown (the grid's
  filter and sort), Parquet and Arrow IPC (.arrows) of the whole result with its exact types.
  e2e `saved.spec.ts`, `roles.spec.ts`, `export.spec.ts` (Excel, Arrow, as shown); unit tests of
  the zip and xlsx writers (a workbook also read back with openpyxl).
- Web UI, the schema browser and autocomplete: Tables and Views groups, column type icons,
  double-click or drag a fully qualified, quoted-where-needed name into the editor, a menu
  (insert or copy a name, preview data in a new tab on that catalog and schema, insert column
  names or a SELECT, a view's definition, use this catalog and schema; not during a
  transaction), search of every table and view the user may see (two or more characters, up
  to 100), re-listing after a run that changes the catalog, Retry on a failed listing, and a
  hideable, resizable side panel (kept in this browser). Autocomplete of catalogs, schemas,
  tables, views, columns (through aliases), functions and keywords from the server's
  metadata, as access control filters it; nothing inside strings or comments. e2e
  `explorer.spec.ts`, `autocomplete.spec.ts` (an RBAC user is offered only what they may see),
  unit tests of the completion parser and quoting.

- Web UI, the results grid: sort by a column (asc, desc, off; NULLs last, typed: exact
  decimals and 64-bit integers, byte-order text), a filter across every column, column
  resizing (drag, double-click to fit, arrow keys), the spreadsheet selection with its whole
  keyboard (arrows, Home/End, Page Up/Down, Ctrl/⌘ jumps, Shift ranges, drag with edge
  scrolling, row numbers, Ctrl/⌘+A, Esc), a copy that pastes into Excel and Google Sheets as
  cells (text plus an HTML table: full values, NULL empty, line breaks inside a cell,
  `00123` kept as text; one cell as its bare value; through the copy command, so Safari
  copies), a cell inspector (double-click or Enter; nested values and JSON text
  pretty-printed with exact numbers; Copy value), Copy of the rows shown, and Expand /
  Restore. e2e `grid.spec.ts` (Chromium and WebKit; with the copy command taken out, its
  WebKit copy test fails); unit tests for the selection moves, the clipboard forms, sorting
  and the JSON printer.

- Web UI, the SQL editor's tabs, runs and sessions:
  up to 20 tabs (Alt+N, Alt+W asking first about an open transaction or unsaved text, rename,
  drag to reorder, running/failed/transaction markers), each its own Warp SQL session; Run
  runs the statement at the cursor (margin bar) or exactly the selection, and Run all
  (⇧+Ctrl/⌘+Enter) every statement, stopping at the first error, with a result tab per result
  set and Messages (at most 50 result sets and 64 MiB kept in the browser); Cancel notes how
  far a script got and an aborted transaction; In transaction / Transaction failed badges with
  Commit and Rollback, from the session document's `transaction`; Catalog and Schema pickers
  (session options, locked in a transaction, following `USE`); row limits 100 to 1,000,000
  (default 1,000) with "truncated at … rows"; the session chip (the server's session id, copied
  on click); New session; a session the server lost is replaced with a note, or, with a
  transaction open, nothing runs and the message says it was rolled back; leaving the page
  with a transaction open asks first; a resizable editor (drag, keys, double-click, kept).
  The editor's tab connections no longer replace a lost session silently. e2e (Chromium and
  WebKit): `tabs.spec.ts`, `run.spec.ts`, `session.spec.ts`; unit tests for the tab store,
  the script runner and the session's loss handling.

- Introspection without instrumentation or metrics (README, Introspection; warp-sql 64eb65c):
  `hardcharger_active_connections()`, a row per open session from the server's own session
  tables: session_id (the Warp SQL `s_...` id), instance_id, cluster_id, username, role,
  tenant, auth_method; the latest request's client_address and client_info (User-Agent);
  session_tags (`MAP(VARCHAR, VARCHAR)`, the Warp SQL `session_tag` option's keys, then
  `SET hardcharger.session_tag`'s); tls, tls_version, certificate_identity,
  certificate_thumbprint, token_certificate_bound; state (`idle`, `queued`, `running`,
  `streaming`, `idle_in_transaction`, `idle_in_transaction_aborted`) and transaction; the
  statement_id and statement (redacted by the statement log's own code), statement_started_at,
  queued_since, session_started_at, last_activity_at; cancel_reason while a cancelled
  statement unwinds; result_partitions, open_result_streams, open_ingest_parts,
  retained_results, retained_rows; is_current. `hardcharger_cancel(session_id)` stops what a
  session runs and keeps it: its statement ends cancelled (`cancel_reason` `cancelled`, also
  in instrumentation), its result readers get an incomplete stream, partitioned results and
  ingest uploads in flight are aborted, and its next statement works; it returns whether
  there was anything to stop. `hardcharger_server_info()`: versions, instance and cluster
  ids, the instance tag, start time and uptime for everyone; the host (hostname, OS, CPU
  model and count, total and resident memory), database path, listen address and ports, TLS
  and mutual TLS, the server certificate's SHA-256 fingerprint, the web UI, the
  access-control store type and the session count for an administrator without
  `catalog_access` rules (NULL for everyone else). It and the instrumentation `instances`
  row come from one collection at startup, which now also records the CPU model (on ARM
  Linux, whose /proc/cpuinfo names none, from the CPU implementer and part: "ARM
  Neoverse-V1").
  Who sees and cancels what: an administrator without `catalog_access` rules, every
  session; anyone else, a restricted administrator included, its own principal's (the same
  user name and tenant), never a more privileged session. `hardcharger_sessions()` follows
  the same rule (a user used to see only the calling session). `test_introspection.py`.

- `result_retention_seconds` (default 3600, from 1 to 2592000): how long a retained
  (resumable) result is kept after its statement ends; the statement document's
  `expires_at` follows it. Flag, `HARDCHARGER_RESULT_RETENTION_SECONDS`, YAML, `--help` and
  `--print-options-json`.

- Instrumentation: `sql_executions.cancel_reason` (and in `execution_details` and
  `session_activity`): why a cancelled execution stopped (`client`, `timeout`, `killed`,
  `cancelled`, `session_closed`, `server_shutdown`). Tables from before get the column at
  startup.

- The session document says where the session's transaction stands (Warp SQL §5.2,
  warp-sql 6afe76d): `GET /warp/v1/sessions/{id}` and the session's creation answer carry
  `transaction`, `none`, `open` or `aborted`, as the engine has it, read after every
  statement, ingest, option change, commit and rollback. `test_rbac_set_role.py` checks it
  for a store user through an allowed, a refused and a failed statement; the conformance
  suite's ten new session tests pass on this server.
- Access-control introspection: table functions `hardcharger_grants()`,
  `hardcharger_role_members()`, `hardcharger_roles()`, `hardcharger_users()`,
  `hardcharger_policies()` and `hardcharger_effective_privileges([user_name])`, read from the
  store snapshot the server holds (no store query per call), and the SQL standard's
  `table_privileges`, `column_privileges`, `role_table_grants`, `applicable_roles` and
  `enabled_roles` with the standard's columns, in
  `_hardcharger_system.hardcharger_information_schema` (DuckDB's `information_schema` can't be
  extended, and this name shadows nothing). The administrator sees everything; a store user
  only grants to itself and the roles it holds, those memberships and roles, its own user
  row and policies on tables it can see; a tenant admin also everything of its own tenant
  (users, roles, memberships, grants, policies, its users' effective privileges) and nothing
  of another's; an administrator token with `catalog_access` rules
  only grants, ownerships and policies inside the catalogs its rules reach. Without an
  access-control store, every function returns no rows. The store records no grant times, so
  there is no `granted_at`. `test_rbac_introspection.py` (23 tests, PostgreSQL and DuckDB
  stores) covers each visibility rule with its positive twin, counting and filtering for
  another user's or another tenant's grants, and the standard views against the table functions.
- Code-signed macOS and Windows releases. In `hardcharger-macos-arm64.tar.gz`,
  `hardcharger-server` and `hardcharger` are signed with GizmoData's Developer ID (hardened
  runtime, secure timestamp) and notarized by Apple, so Gatekeeper accepts them; the server's
  only entitlement turns library validation off, so DuckDB can still load extensions a user
  `INSTALL`s. On Windows, `hardcharger-server.exe` and `hardcharger.exe` in both zips, and both
  MSIs, carry an Authenticode signature (SHA-256, RFC 3161 timestamp) from GizmoData's
  code-signing certificate in Azure Key Vault. Every `v*` tag signs, and a manual
  `server.yml` run with `sign` signs without publishing; the tests run on the unsigned
  binaries, which are signed before they are packed, so the tarball, the zips, the MSIs and
  `SHA256SUMS` all hold the signed files. Two jobs check the artifacts the release takes
  (`codesign`, the notarization ticket and `spctl` on macOS; `Get-AuthenticodeSignature` and
  `signtool verify /pa` on Windows on Arm, which also runs both platforms' signed
  executables), each first shown to refuse a modified binary, and a tag publishes nothing,
  and its release job fails, unless both pass. See "Code signing" in `docs/releasing.md`.

### Changed

- **Instrumentation keeps up**: the writer records tens of thousands of statements a second
  and stays as fast however large its tables grow. It wrote three statements per execution
  (insert the statement, insert the execution, update it at the end), each its own
  transaction, and each update scanned the whole `sql_executions` table, which has no index
  in the DuckDB file: on a laptop it managed ~580 statements a second into an empty file and
  ~250 with 5 million rows (a 100,000-statement run took 169 s after the clients finished),
  and PostgreSQL, through the postgres extension, ~130 a second, ~24 with 2 million rows
  (the extension read the table to find each row). Now it writes in
  batches, one transaction each: every event queued since the last write, with an
  execution that starts and ends within a batch folded into one row, rows of earlier
  batches updated by one statement per table, and the rows travelling as one JSON literal.
  In the DuckDB file each update reads only recent row groups (bounded by the start time);
  PostgreSQL gets native SQL, one round trip per batch, that its keys serve. Measured with
  `server/tests/bench_instrumentation.py` (16 sessions): the DuckDB file records every
  statement as fast as the clients run them (~22,000 a second) with 5 million rows already
  in the tables, and its writer alone does ~120,000 a second; PostgreSQL ~20,000 a second
  with 2 million rows, also as fast as the clients ran (~29,000 for the writer alone); the
  backlog is written within 0.2 s of the clients stopping. The DuckDB file gets no indexes: an ART key made inserts 2.5x
  slower and holds memory, for no faster update.
- Instrumentation's queue is bounded (128 MiB of records): past it, new records are dropped
  rather than held, counted in `hardcharger_server_info()` (`instrumentation_queued`,
  `instrumentation_dropped`, `instrumentation_failed`, administrators without
  `catalog_access` rules) and on `/metrics` (`hardcharger_instrumentation_queue_depth`,
  `hardcharger_instrumentation_dropped_total`, `hardcharger_instrumentation_failed_total`),
  and logged at most once a minute. A statement never waits for its record. At shutdown
  the backlog is written for up to 30 seconds; what is left then is dropped and counted,
  and the instance's sessions and executions are closed in the tables all the same.
- A batch that fails is retried in halves, so one bad record loses only itself; text that
  isn't valid UTF-8 (a statement cut at 4,000 bytes can split a character) is stored with
  U+FFFD instead of failing its row. In PostgreSQL, a statement or execution whose parent
  row was never written (dropped from a full queue) is left out instead of failing its
  batch on the foreign key.
- A session's `stop_time` is when it ended, taken by the server, not when its row was
  updated.

- **One id everywhere, for statements too**: instrumentation keys statements and executions
  by the Warp SQL ids clients hold, not by UUIDs of the server's own, so a `q_...` from
  `Warp-Statement-Id`, an error response or `hardcharger_active_connections()` finds the
  statement in `_hardcharger_instr`. `sql_executions.execution_id` is the id of the request
  that ran it (`q_...`); `sql_statements.statement_id` is that same `q_...` for a submitted
  statement (statement and execution share it) or the prepared statement's `p_...` (one row,
  with an execution per execute request, each under its own `q_...`). A request carrying
  several statements is one statement row (`q_...`) whose executions are numbered by place in
  the script: `q_....1`, `q_....2`, ... (a `SET hardcharger.*` or `KILL SESSION` among them
  takes its number but runs no execution); one statement alone has no suffix. Statements the
  server runs for itself (init SQL, access-control internals, bookkeeping) are still not
  recorded. The columns are `VARCHAR` (`instance_id` and `cluster_id` stay UUIDs), in the
  DuckDB file, DuckLake and PostgreSQL schemas alike; a table with UUID statement or
  execution ids is refused at startup with what to do (drop or rename the instrumentation
  tables, or use a new instrumentation database). The query log's lines carry the same ids:
  `statement_id` (`q_...`, or `q_....N`) on statement, success and failure lines, and
  `prepared_id` (`p_...`) on prepare lines and on a prepared statement's executions. The SDK
  tells the engine these ids (warp-sql `feat/instrumentation-warp-ids`:
  `EngineSession::SetStatementId()`, and the prepared id is chosen before preparing).
  `test_instrumentation.py` looks up a submitted statement, a prepared statement's three
  executions, a three-statement script, failed, refused and cancelled statements and the log
  lines by the ids the client got, and checks that both kinds of old table are refused; the
  same tests fail against the previous server.

- **Browser sign-in moved into the Warp SQL protocol, on the main port** (Warp SQL §4.3,
  capability `oauth_browser`; the SDK at warp-sql `feat/oauth-in-protocol`). Clients create a
  flow (`POST /warp/v1/auth/oauth/flows`), the person signs in with the identity provider,
  which redirects to `GET /warp/v1/auth/oauth/callback`, and the client collects
  Hardcharger's own access token once with the flow's poll secret (never in a URL); the
  provider's ID token no longer reaches clients. The server sends the provider `state`, PKCE
  (S256) and a `nonce` (checked in the ID token), exchanges the code as before, and signs the
  user in as `token_exchange` does (JWKS, issuer, audience, `token_authorized_emails`, role,
  store users); the session's `auth_method` is `oauth_browser`. **The redirect URI to register
  with the identity provider is now `<oauth_base_url>/warp/v1/auth/oauth/callback`**:
  `oauth_base_url` is the server's public URL (default `http(s)://localhost:PORT`), and
  `oauth_redirect_uri` still overrides it. `oauth_instance_id` still prefixes the state for a
  shared callback proxy, and with `oauth_base_url` set the redirect back to a web page is
  absolute on it. New: `oauth_provider_name` (discovery's `auth.oauth_browser.provider`, the
  UI's "Sign in with NAME") and `oauth_max_flows` (sign-ins in progress at once, default
  10000, and at most 100 per client address; beyond, `429 rate_limited`).
  `hardcharger_server_info()` has `oauth_enabled` (BOOLEAN) for `oauth_port`.
- `hardcharger --auth-type external` uses that flow on the server's own port and says which
  provider it waits for; it refuses a server without `oauth_browser`.
- Web UI: **Sign in with <provider>** (or "single sign-on") beside the user name and password
  when the server offers browser sign-in: the page starts a flow with `return_to` set to
  itself, keeps the poll secret in the tab, and on its return (the flow id in the URL
  fragment, removed at once) collects the token or shows why the sign-in failed.
- **Warp SQL 1.0.0** (warp-sql `spec/v1.0`). The protocol's shape fixed for its first stable
  version, with no fallbacks for the drafts:
  - A statement that fails while running is `422 execution_error` (was `500`); `500` is now
    `internal_error`, for the server's own faults (an engine internal error, a result
    Hardcharger itself could not build). `statement_cancelled` and `statement_timeout` are
    `409`, and a statement cancelled before its submit is answered (a `KILL SESSION`,
    `hardcharger_cancel()`, its timeout, a session cancel) answers with that error and its
    `cancel_reason`, no longer a `200`/`201` document.
  - Server-info keys of Hardcharger's own take its prefix: `hardcharger_instance_id` and
    `hardcharger_access_control` (were `instance_id` and `access_control`), `duckdb_version`
    (was `engine` and `engine_version`), and `server_read_only` says whether the server
    runs read-only; `protocol` and `protocol_version` are gone (discovery has them). The
    client's `.info` reads `duckdb_version`.
  - The Warp SQL session option is `duckdb_arrow_lossless_conversion` (was
    `arrow_lossless_conversion`; the SQL `SET SESSION arrow_lossless_conversion` is DuckDB's
    and keeps its name).
  - Discovery has no `draft` field, and no `result_retention` capability (retention is core);
    a partitioned ingest commits with `{"part_count": n}`; a partition the result doesn't have
    is `partition_not_found`; `client_certificate_required` is `certificate_required`.
  - `Warp-Query-Tag` (an ASCII JSON object, at most 4 KiB) is recorded as a submitted
    statement's query tag in the instrumentation tables, ahead of `SET hardcharger.query_tag`;
    a malformed one is refused with `400`.
  - The command-line client's own error codes take the protocol's `client_` prefix
    (`client_connection_failed`, `client_tls_failed`, `client_protocol_error`, …), and so do
    the web UI's (`client_transport_error`, `client_protocol_error`, `client_sign_in_failed`).
  - Web UI: the vendored JavaScript client moves to warp-sql-js `4a27d4d` (Warp SQL 1.0.0, on
    browser sign-in); browser sign-in's own failures take the `client_` codes too, and a flow
    state the page doesn't know is polled on (Warp SQL §12) rather than refused; the
    About dialog drops the draft label, an `invalid_token` or `session_ended` with
    `token_revoked` says the sign-in was revoked, a reason the UI doesn't know reads as an
    expired sign-in, and a statement timeout says so.
  - Without a signing key, `CREATE SCOPED TOKEN` is `501 not_implemented` (was
    `execution_error`).
  - CI: the conformance suite's new one-signal drain and access-control tests run against
    Hardcharger (`--shutdown-grace-period-seconds`, the DuckDB access-control store).

- `KILL SESSION` runs `hardcharger_cancel()`'s cancel first (one implementation), then ends
  the session: what it runs stops (within milliseconds for a CPU-bound statement), its
  statement ends cancelled with `cancel_reason` `killed` (it used to fail with
  `session_ended`), its result streams are cut off, its partitioned ingests and uploads are
  aborted, and then every request naming it, a statement's status or result included, gets
  `session_ended` (`killed`); a DELETE of it answers 204. Ending alone never stopped the
  work: tests fail with the cancel taken out of KILL's path.

- An error a function raises while the rows are read, when it is DuckDB's `Permission
  Error`, is `permission_denied` (403), not `execution_error`.

- Store users (tenant users included) read the views in `_hardcharger_system`
  (`hardcharger_index_info`, `hardcharger_view_definition` and the new ones) without a grant:
  the catalog is read-only and its views read with the reader's rights, so each lists only
  what that user may see. They used to be refused (no SELECT privilege on the view), while
  `catalog_access` principals could already read them.
- The transaction a session reports (the session document, and the
  `hardcharger_sessions` metric's `idle_in_transaction` and `idle_in_transaction_aborted`
  states) is the one DuckDB has: a syntax error inside a transaction leaves it open
  (DuckDB forgives it), where any failure used to count as aborted. The server's own
  tracking is gone; it reads the Warp SQL SDK's (`test_metrics.py` covers the syntax
  error).

- With `autocommit` false, a SQL `COMMIT` or `ROLLBACK` starts the next transaction, as
  `POST .../commit` and `.../rollback` do (warp-sql §5.5); the session used to drop out of
  manual-commit mode after one.

- Web UI: the vendored `@gizmodata/warp-sql` is warp-sql-js d2fb3b7 (was 2b96f0a), with
  `transactionState()` and the session document's `transaction`, plus the client's changes
  in between: an explicit port 443 kept on plain HTTP, exact wide integers in JSON
  parameters, and client-certificate sign-in through the client.
- Releases take release-candidate tags, `vX.Y.Z-rc.N` (the next is `v2.0.0-rc.1`), as well as
  `vX.Y.Z`. `server/CMakeLists.txt` holds the suffix in `HARDCHARGER_VERSION_SUFFIX`, and every
  reported version (`--version` of the server and the client, `HARDCHARGER_VERSION()`, discovery,
  server-info, instrumentation, the web UI and its tarball's name, the Windows version strings)
  is the full `2.0.0-rc.1`; the MSI's numeric ProductVersion is `2.0.0.1`. A candidate is a GitHub
  pre-release and pushes images `:X.Y.Z-rc.N` and `:sha-<short>`, never `:latest`; a release
  still pushes `:X.Y.Z` and `:latest`. The release notes come from the CHANGELOG heading matched
  as text, not as a regex (its dots used to match any character), and a tag whose version or
  CHANGELOG section is wrong now fails in the workflow's first job instead of after the matrix.
  The version arithmetic is `.github/scripts/release.sh`, tested by `test-release.sh` on every
  run; docs/releasing.md describes the process.

- Faster remote throughput (warp-sql d25f797, warp-sql-adbc da6b4a6; docs/benchmarks.md,
  2026-10-19): compressed results compress only the buffers a sample shows are worth it
  and send the rest by reference; a split result's partition count follows its size; the
  Go/Python driver decompresses batches in parallel, defaults to `auto` compression
  (lz4_frame near, zstd far), up to 32 result partitions and `auto` ingest parts, and
  uploads in 16,384-row slices.

- Connections set TCP_NODELAY (warp-sql 58082fa): every small request (discovery, a status
  poll, `SELECT 1`) waited ~40 ms on Linux for the client's delayed ACK.

- `bench/remote/serve.sh` stops each server by the PID it recorded, never by pattern.
  New: `run_cases.sh` (a list of cases against one system), `build-from-source.sh` (build
  the server or driver on a benchmark VM), `midsize.py` (partition overhead on mid-sized
  results).

- The Hardcharger™ bull logo. The brand kit (logos, wordmark, icons, social images; light and
  dark variants) is in `docs/assets/brand/`. The README's banner is the horizontal mascot
  lockup, light or dark to match the reader's GitHub theme (it was a drawn SVG card), and its
  web UI screenshots are refreshed. The web UI shows the head lockup in its header and the
  stacked mascot on the sign-in page and at the top of About (the original raster art, as
  WebP; the server now serves `.webp` as `image/webp`), each in its light or dark variant to
  match the theme; About also says "© <year> GizmoData LLC. All rights reserved.", with the
  year of the build. Its icons are new (`favicon.ico`, `favicon.svg`, `apple-touch-icon.png`)
  with a web app manifest (`manifest.json`, 192 and 512 px icons), in place of `favicon.png`.
  The embedded UI grows by about 300 KiB gzipped. On Windows, `hardcharger-server.exe` and
  `hardcharger.exe` carry the bull's head as their icon, and the MSI shows it in Installed apps
  and in its dialogs' banner and welcome panel.

- `KILL SESSION` naming the session that runs it is still refused, now with SQLSTATE `55000`
  (object not in prerequisite state; it was `42P01`, undefined table) and "A session can't
  kill itself; disconnect (or close the connection) instead." (it was "Cannot kill your own
  session"). `test_admin_cannot_kill_own_session` checks both, and that the session still works.

### Removed

- **DuckLake as an instrumentation backend.** Instrumentation has two backends: the DuckDB
  file next to the database (the default, for development or a single server) and
  PostgreSQL, recommended in production and for any cluster of more than one replica. An
  `instrumentation_catalog` that names a DuckLake catalog (or any catalog but PostgreSQL)
  now stops the server at startup with what to use instead. Catalog logging still writes to
  DuckLake.

- The separate browser sign-in server and its port (`oauth_port`, default 31339), its
  GizmoSQL 1.x routes (`/oauth/initiate`, `/oauth/start`, `/oauth/callback`,
  `/oauth/token/<uuid>`) and `oauth_disable_tls` (`--oauth-port`, `--oauth-disable-tls`,
  `HARDCHARGER_OAUTH_PORT`, `HARDCHARGER_OAUTH_DISABLE_TLS`). The client's `--oauth-port`,
  `--oauth-base-url`, `HARDCHARGER_OAUTH_PORT` and `HARDCHARGER_OAUTH_BASE_URL` are gone too.
  A redirect URI registered for the old port must be replaced (see above).

### Fixed

- `--licenses` on Windows prints the notices byte for byte, as on Linux and macOS: the Windows
  checkout had turned `THIRD_PARTY_NOTICES.md` to CRLF line ends (git's `core.autocrlf`), so
  the binaries embedded those, and stdout in text mode then wrote `\r\r\n`. A
  `.gitattributes` keeps the file LF in every checkout, and both binaries write the notices
  with stdout in binary mode.
- The server builds with clang against libstdc++ 14 (Ubuntu 24.04's): warp-sql's build-time
  OpenAPI tool instantiated a `std::pair` of an incomplete type, which libstdc++ 14 refuses
  (warp-sql `0e8dc87`; the served document is unchanged).
- From the warp-sql 1.0.0 release branch (`third_party/warp-sql` at `release/v1.0.0`): a
  retained result is expired from its `expires_at` on (Warp SQL §6.5), where the SDK refused
  new reads only after its next sweep, up to 5 seconds later; and an idempotent replay of a
  submit or execute (§6.7) carries `Warp-Statement-Id`, as every statement response does.
- Web UI: once the editor's tabs no longer fit, clicks on them and on **New tab** work in
  Safari. The tab bar scrolled as a whole, and Safari's overlay scrollbar sat over the bottom
  of the tabs and the bottom-aligned **New tab** button and took their clicks (the e2e "at most
  20 tabs" test lost 1 or 2 tabs in about 40% of WebKit runs). Only the tabs scroll now, with
  no visible scrollbar (a mouse wheel, a sideways swipe or keyboard focus scrolls them), the
  shown tab is always scrolled into view, and **New tab** and the help button stay put after
  them, outside the scrolling part (and outside the `tablist`, which now holds only tabs).
- The Windows zips no longer carry jwt-cpp's headers (`include/jwt-cpp/`) and CMake package
  files (`cmake/`): the package step took the whole `cmake --install` tree, which jwt-cpp's
  install rules add to; it now copies the two executables, and checks the zip's top level.
- A retained result that expires (`result_retention_seconds`) while a client is reading it
  no longer cuts that read: the read gets every batch and the end of the stream, and only
  reads that start afterwards get `410 result_expired`. The expiry sweep (every 5 s)
  dropped the batches at once, so a read in progress ended without the end-of-stream
  marker ("incomplete chunked read") for a statement that had succeeded; Windows CI hit it
  in `test_result_retention_seconds_sets_expires_at_and_the_expiry`. The batches are now
  freed when the last read that started before the expiry ends. Warp SQL SDK fix (the
  `third_party/warp-sql` bump); `test_a_read_in_progress_when_the_result_expires_runs_to_the_end`
  stalls a 64 MB read across the expiry.

- The sensitive-path guard no longer refuses harmless string literals. It used to scan the
  SQL text for literals that looked like paths, and a literal with no directory part is
  relative to the working directory, so a server whose working directory holds a protected
  path (the Docker image's is its home, with `~/.ssh` in it) refused, as "user credential
  store", every literal with a `{` (JSON, regex quantifiers such as `'^[0-9]{3}$'` or
  `'(\d{2})'`, `format('{:,}', n)`), `'*'` and `'.*'`, in queries, views and masking
  policies alike. SQL text is no longer scanned for paths at all (see Security), so
  literals, `'~/.ssh/id_rsa'` used as a value included, work as anywhere else.
  `test_sensitive_path_fs_layer.py` and `test_sensitive_path_guard.py` cover them, with the
  server's working directory a home holding `~/.ssh`.

- The `hardcharger` CLI exits when a script starts it in the background
  (`hardcharger ... -c 'SELECT 1' &`). A non-interactive shell starts background jobs
  with SIGINT ignored, and the client's Ctrl-C watcher was woken at exit by a SIGINT to
  its own thread, which an ignored SIGINT never delivers: the client printed its result
  and then waited forever. With SIGINT ignored from the start the client starts no
  watcher (there is no Ctrl-C to take); if it is ignored later, shutdown restores the
  default action for its one wake signal and then puts the ignore back. Ctrl-C still
  cancels a running statement. `test_client_cli.py` runs a background job four ways
  (plain, stdin from /dev/null, from a closed pipe, a script on a pipe), and two client
  unit tests cover the watcher with SIGINT ignored.

- CREATE VIEW in catalog `x` over a schema also named `x` (`CREATE VIEW x.x.v AS SELECT *
  FROM x.x.t`) fails, as it does on plain DuckDB 2.0 (DuckDB 1.5 creates it): DuckDB drops
  the view's own catalog from the names in its query and then finds `x.t` ambiguous.
  Hardcharger's error now says what happens and what works (create the view in another
  catalog, or rename the schema), where DuckDB's message said to qualify a name that
  already was; `docs/migrating-from-gizmosql.md` documents it.
  `test_view_catalog_schema_same_name.py` covers it with access control on and off.

### Security

- A failed instrumentation or catalog-log write no longer logs the password of the
  catalog's connection string: PostgreSQL's connection errors quote it
  (`Unable to connect to Postgres at "host=... password=..."`), and the warning now masks
  it as the query log does (`password=***`).
- The private-file guard now also covers extensions that open files outside DuckDB's file
  system. `sqlite_scanner` opens the file with SQLite's own library, so `ATTACH '<path>'
  (TYPE sqlite)`, the `sqlite:` path prefix, and the `sqlite_scan` / `sqlite_query` /
  `sqlite_attach` table functions bypassed the file-system guard; a SQLite file created
  where the database's WAL goes made a committed INSERT vanish after a crash and restart.
  These are refused at plan time, once the path is resolved, whenever it names one of the
  server's own files or a sibling (the store, database, instrumentation and log files, with
  their WAL and temporary siblings), by every spelling (computed, `..`, relative, symlink,
  hard link, case, Windows 8.3). Attaching an unrelated SQLite file still works. DuckDB-
  storage attaches are unchanged (the file-system guard already covers them). Tested as the
  administrator and as a file-system-privileged user, each with a positive twin.
- The sensitive-path guard (`block_sensitive_paths`, on by default) now checks every path
  in DuckDB's file system, as GizmoSQL 1.x's does, instead of scanning the SQL text for
  path literals. The text scan missed every path the statement computes: `query('…')`, a
  table macro's or view's argument, a prepared statement's parameter, a variable, a
  concatenation, `file_search_path`. So an administrator, whom the guard holds too, could
  read `~/.ssh`, cloud CLI credentials, `/etc/shadow` or `/proc` by spelling the path
  indirectly. Now every open, stat, listing, glob, copy, move, remove and
  directory change is checked by the path DuckDB resolved, for every principal, after
  `..`, `~`, symlinks, second hard links and case folding: a glob reaching into a protected
  directory from above leaves those matches out, and one whose fixed part is protected is
  refused. The `sqlite_scanner` plan-time guard also refuses protected paths (`ATTACH …
  (TYPE sqlite)`, `sqlite:`, `sqlite_scan`, `sqlite_query`, `sqlite_attach`). `SET
  secret_directory` stays refused after startup. Not checked, as before: the server's
  startup and the operator's init SQL, and files `postgres_scanner`'s libpq reads itself
  (`~/.pgpass`, `passfile=`, `sslkey=`); the README says so.
  `test_sensitive_path_fs_layer.py` runs against a fake home of dummy credential files:
  each way of reaching a file, as the administrator and as a file-system-privileged user,
  is refused, and each has a twin on a server with the guard off that reads the file.

### Documentation

- README: "Why Hardcharger instead of Quack?", a feature comparison with DuckDB's Quack
  extension (`e7a9be3f8c`, the commit DuckDB 2.0 pins), and docs/hardcharger-vs-quack.md with
  the versions and the checks behind each row, run against live Quack and Hardcharger 0.3.0
  servers. Features only, no throughput figures. The doc also shows DuckDB attaching
  Hardcharger through an ADBC extension (Columnar's `adbc` or Query.Farm's `adbc_scanner`) with
  hardcharger-adbc's driver library, with examples run against Hardcharger 0.3.0.
- README: "Where Hardcharger is faster", under "Why Hardcharger instead of Quack?": the five
  shapes of the 2026-10-19 benchmark where Hardcharger beat Quack (SF1 fetch and load over the
  LAN and the WAN, SF10 fetch over plaintext), with the conditions, and a link to the full
  results, including the shapes where Quack is faster. Each median recomputed from
  `bench/remote/results/2026-10-19-throughput-2/`.

- Bulk ingest answers `415 unsupported_arrow_type` (Warp SQL §8.1) for an Arrow type
  DuckDB can't keep, where `decimal256` (any precision) and dense unions, also nested in a
  list, struct or dictionary, were a `400 sql_error` from inside DuckDB. warp-sql e354e97:
  the DuckDB backend lets through only the types DuckDB's Arrow scan reads and refuses
  any other before a table is touched. `test_bulk_ingest.py` and the conformance suite's
  two new ingest tests cover it.

- `--metrics-port N` (or `HARDCHARGER_METRICS_PORT`, or `metrics_port` in the file)
  without `--enable-metrics` served nothing, and the ready line said `metrics_port=`.
  Saying where `/metrics` listens now turns metrics on: a `metrics_port` other than 0, or
  a `metrics_bind_address`, sets `enable_metrics` unless it is set itself. An explicit
  `enable_metrics` false still wins, and the startup log says the port is unused. The
  ready line says `metrics_port=off` when metrics are off and `none` for metrics in SQL
  only (`metrics_port` 0), and `oauth_port=off` without the OAuth server.
  `test_metrics_configuration.py` covers each source and case.

- A session has one id: the Warp SQL session id its client holds (`s_...`, from the
  session document). `KILL SESSION '<id>'` with that id said "not found", because the
  server kept a UUID of its own, which only `HARDCHARGER_CURRENT_SESSION()` showed.
  `HARDCHARGER_CURRENT_SESSION()`, `hardcharger_sessions()`, `KILL SESSION`, the log
  lines' `session_id`, the instrumentation tables (`sessions`, `sql_statements` and the
  views over them) and the catalog log's `session_id` all carry the Warp SQL id now
  (warp-sql e1775b3: the SDK hands it to the engine). Those `session_id` columns are
  `VARCHAR`; an instrumentation or log table that Hardcharger 0.3.0 created with UUIDs is
  refused at startup, saying to drop it or use a new database (its rows' ids match no
  session anyone holds). `test_kill_session.py` checks one session's id in the session
  document, `HARDCHARGER_CURRENT_SESSION()`, `hardcharger_sessions()`, the instrumentation
  tables, the log and `KILL SESSION`.

- No GizmoSQL-era (Flight SQL) names in logs or instrumentation: bulk-ingest log lines
  carried `flight_method=ingest` beside `kind=ingest` (now only `kind`), and the
  instrumentation table `sql_statements` named its column `flight_method`; it is
  `statement_kind` (`statement` or `prepared`, the Warp SQL endpoint a statement came by).
  A 0.3.0 instrumentation table is refused at startup in any case, as its session ids are
  UUIDs (above: drop it); so is one whose `session_id` is already `VARCHAR` but still has
  `flight_method`, as its statement ids are UUIDs (Changed: one id everywhere). No
  metric label had the name. `test_instrumentation.py`
  and `test_bulk_ingest.py` cover it.

- `INSTALL` works without `HOME` (services, containers): DuckDB keeps extensions and
  persistent secrets under `HOME` and, without it, refused every `INSTALL` ("Can't find the
  home directory at ''", a 500) and kept persistent secrets relative to the working
  directory. With `HOME` unset, DuckDB's `home_directory` is now the account's home (the
  password database, the home the sensitive-path guard already protects); with no
  writable home at all (also `HOME=/` for a container run as a user the image has no
  account for), `extension_directory` and `secret_directory` are `.duckdb/extensions` and
  `.duckdb/stored_secrets` in the database file's directory (the working directory for an
  in-memory database). The startup log says which, and warns when neither is writable.
  `test_extension_directory.py` covers both cases with `HOME` unset or unusable.

- `hardcharger`: after a `KILL SESSION` (or another `session_ended`), the statement it caught
  printed the server's "Sign in again and open a new session to re-authenticate" right below
  the client's "a new session is open". The client has done both by then; it now says "The
  statement did not complete: its session ended (killed). It was not run again; run it again
  to run it in the new session." `test_client_cli.py` checks the whole message.

- Tests now pick listen ports collision-free across concurrent runs on one machine. Each
  server's port was taken from a fixed range (20000+) split only between one run's
  pytest-xdist workers, so a second run (another checkout, a non-xdist run) reused the same
  ports: a bind failed with "Address already in use", or a client signed in to the other
  run's server and got `invalid_credentials`. `free_port()` now holds a machine-wide
  advisory lock on each port it hands out (a lock file named after the port, released when
  the process exits) and still probes that nothing is bound, so two full suites run at once.
  (The server also logs its bound port in its ready line, for a run that lets the OS choose.)

- `lz4_frame` Arrow buffer compression beside `zstd` (Warp SQL §8.2, capability
  `ipc_compression_lz4_frame`), with the client's codec preference list in
  `Warp-IPC-Compression` (warp-sql d25f797).

## [0.3.0] - 2026-10-18

A built-in web UI, client certificates as identity (mutual TLS), a DuckDB-file
access-control store for single servers, faster remote transfer (split results and
partitioned uploads by default), Windows builds, and the `hardcharger` command-line client,
TLS by default. Security fixes from adversarial reviews: metadata follows privileges,
`CREATE VIEW` needs SELECT on what a view reads, `catalog_access` holes, and SQL can no
longer reach the server's own files (see Fixed).

### Added

- A refused token says why (Warp SQL §4.2): after `ALTER USER ... WITH PASSWORD`, `ALTER
  USER ... DISABLE`, `DROP USER` or `DROP SCOPED TOKEN`, the session's own token gets `401
  invalid_token` with a `reason` (`credentials_changed`, `principal_dropped`,
  `token_revoked`), and an expired token `token_expired`. Before, only a token from a new
  sign-in, used on the old session, learned the reason (from `session_ended`). Only a
  token this server signed and that nobody altered gets a reason; one signed with another
  key or with a changed signature gets none, whatever user it names. A session opened with
  a scoped token now ends when the scope expires (`session_ended`, `token_expired`)
  instead of refusing each statement. `test_token_refusal_reasons.py` (8 tests), and the
  conformance suite's expired-token case.

- `hardcharger`: when the server refuses the sign-in's token it says why, from the reason,
  e.g. "The server no longer accepts this sign-in: the user's password or credentials
  changed (credentials_changed). Sign in again (.connect)."; a session the server ended
  reads "the server ended this session: an administrator ended it (killed)".

- `hardcharger`: HUGEINT and UHUGEINT print as exact integers with
  `arrow_lossless_conversion` on (DuckDB's lossless form, Arrow's opaque 16-byte type), in
  every output mode (unquoted numbers in JSON, right-aligned in boxes, type
  `hugeint`/`uhugeint`); they printed as raw bytes.

- Allowed CORS origins (`cors_origins`) get `Access-Control-Allow-Credentials: true`, preflights
  included (Warp SQL §9.5), so a page on another origin can sign in with the user's client
  certificate (`credentials: "include"`). Other origins still get no CORS headers. The README's
  mTLS section says such pages need `mtls_require_client_cert: false` (a preflight carries no
  certificate). From warp-sql `ec318ff`; CI runs the conformance suite's credentials tests with
  `WARP_SQL_TEST_SERVER_CORS_FLAG=--cors-origins`.

- Client certificates as identity (Warp SQL §4.4): with `mtls_ca_cert`, the
  `client_certificate` grant signs a client in with its verified certificate and no
  password. `mtls_identity_source` (`san_email`, the default; `san_uri`; `subject_cn`) says
  where the identity comes from; a certificate with none or several values is refused.
  `mtls_admin_identities` sign in as the administrator; under access control any other
  identity must be an enabled store user (`CREATE USER "joe@example.com"`, no password
  needed), and grants decide what it may do; without access control nothing else signs in.
  The tokens carry RFC 8705's `cnf` (`x5t#S256`) and work only on connections presenting the
  same certificate. `mtls_crl_file` refuses revoked certificates at the handshake, is reloaded
  when the file changes (looked at every 5 s), and reaches open connections at their next
  request. `mtls_require_client_cert` (default true) and `mtls_require_identity_match`
  (default false: a password or token sign-in over a certificate must be for its identity).
  Certificate sign-ins, allowed and refused, are audit records (`kind: authentication`,
  `method: client_certificate`, the identity, subject, issuer, serial and thumbprint).
  A session opened with a bound token is used only with tokens bound to the same certificate,
  and a scoped token (`CREATE SCOPED TOKEN`) minted over a bound token carries its `cnf` too.
  `hardcharger_sessions()` shows `client_certificate` as the auth method. Tests:
  `test_mtls_identity.py` (42), and the conformance suite's `test_mtls.py` (41).
- From the security review: certificate sign-in is opt-in (`mtls_certificate_sign_in`,
  default false; `mtls_admin_identities` needs it), so `mtls_ca_cert` alone keeps mTLS a
  transport check. CRLs cover every certificate of the chain, and a CA without one is refused
  unless `mtls_crl_allow_missing_ca_crls`; open connections are re-checked when a CRL passes
  its nextUpdate. Identities are printable ASCII. A session takes only tokens bound as its
  opener's was, so a scoped token always carries the binding of the request that minted it,
  and a bound session can't mint `FOR USER` another user. Certificate refusals are security
  warnings and audit records (user, token id, thumbprint, peer), and a token is logged as
  validated only after its certificate checks.
- Second review: SPIFFE IDs (`spiffe://…`, any URI-form name) can be access-control users;
  they are always system scope, a tenant admin can't create or drop one, and a role can't be
  named like one. Certificate refusals are rate-limited per source by the SDK (summaries carry
  `suppressed`), and nothing else logs them per request; the handshake refusing a client
  certificate is an audit record (`the TLS handshake refused a client certificate`). CRL
  files: the newest CRL per CA, a CA's CRL must be signed by it, no delta CRLs (the SDK).
- Third review: handshake refusals are rate-limited by the client's address (IPv6 /64), not
  by the unverified certificate, and every client-supplied field of a refusal record is cut
  at 512 bytes; a refused password sign-in logs at most 256 bytes of the user name. A failed
  CRL reload is retried. A scoped token bound to a certificate can't be exchanged (as user
  `token`'s password) over another one. URI identities (SPIFFE IDs) must match a user name
  exactly, case included; e-mail and CN identities still ignore ASCII case. CI runs
  `warpsql-sdk-tests` (the limiter's unit tests), under TSan too.
- The `hardcharger` client: `--auth-type mtls` (or `authType=mtls` in a URI) signs in with
  the `--mtls-cert` certificate, and signs in again with it when the server ends the session.
- The built-in web UI: `web_ui` (`--web-ui`, `HARDCHARGER_WEB_UI`, off by default) serves a
  browser UI at `/ui` on the server's own listener (same port, origin and TLS certificate as
  Warp SQL): a SQL editor with tabs and cancel, a results grid that copies in every browser,
  a schema browser that follows access control, CSV/TSV/JSON/Parquet exports, the session
  list, and sign-in with a password or, under mutual TLS, a client certificate. Its source is
  in `ui/` (folded in from the hardcharger-ui repository with its history), styled in
  GizmoData's brand with Inter embedded (SIL Open Font License 1.1), with Hardcharger™ and
  GizmoData™ marked on the wordmark, the badge and a trademark notice in its footers. CI builds it once per
  run and every server build in that run embeds it (`-DHARDCHARGER_WEB_UI`): CMake checks each
  file against the tarball's manifest, embeds it gzipped and logs the content SHA-256. The
  server answers gzip to clients that accept it and inflates (zlib 1.3.2) for those that
  don't, with an ETag per representation and `304` for `If-None-Match`. Answers follow the
  UI's serving contract: `index.html` for the app's routes under `/ui/`, hashed assets
  immutable, everything else revalidated, 404 (never cached) for missing files, and a strict
  Content-Security-Policy and security headers on every answer; nothing at all when off.
  A build without the UI refuses `web_ui`. The Warp SQL SDK gained `plain_handler`, through
  which the server answers GET and HEAD outside `/warp`, with the request's headers. Under
  mutual TLS the page needs a client certificate like any client, and offers certificate
  sign-in when `mtls_certificate_sign_in` is on. Tests: `test_web_ui.py` and
  `web_ui_test.cc` on the server; the UI's lint, typecheck, unit tests and Playwright e2e in
  Chromium and WebKit against the server built in the same run. Releases attach the UI's
  own tarball (`hardcharger-ui-<version>.tar.gz`) and its content SHA-256. The Windows
  builds embed it too.

- `hardcharger`, the command-line SQL client: GizmoSQL 1.x's `gizmosql_client` on Warp
  SQL, with the same options (in Hardcharger's names: `HARDCHARGER_HOST`, `_PORT`, `_USER`,
  `_PASSWORD`, `_TLS`, `_TLS_ROOTS`, `_OAUTH_PORT`, `_SESSION_TAG`, `_QUERY_TAG`;
  `hardcharger://` URIs; `~/.hardchargerrc`, `~/.hardcharger_history`), dot commands, output
  modes, heredoc/pipe/`-c`/`-f` scripting and exit codes, the pager, history, completion,
  syntax highlighting, query and session tags, TLS and mutual TLS, and the server's
  browser sign-in (`--auth-type external`). Ctrl-C cancels the running statement through
  Warp SQL's cancel and the shell goes on. It ships in the tarballs' `bin/` next to
  `hardcharger-server`, and the image has both on `PATH` (the entrypoint stays the server).
  docs/migrating-from-gizmosql.md maps the names and lists what behaves differently.
- GizmoSQL 1.x's client tests, ported: `test_interactive_client.cpp` (115 cases, as C++
  against the client's classes), `test_client_cli.cpp` (18), `test_client_config.cpp` (5),
  the 4 client cases of `test_instance_session_query_tags.cpp`, and `test_client_shell.sh`
  (64 assertions), all against a real hardcharger-server; plus the shell at a terminal
  (pexpect), TLS, mutual TLS, browser sign-in and session-recovery cases.
- replxx (release-0.0.4), the client's line editor, built by `scripts/build-deps.sh` and
  cached with DuckDB and Arrow.
- Results split into partitions that a client reads at once over separate connections
  (Warp SQL §6.10, capability `result_partitions`): `max_result_partitions` (default 32,
  1 never splits) caps the split. With Warp SQL's drivers' `result_partitions` option,
  one large result uses several network flows and client cores.

- `hardcharger-server --print-options-json`: every option with its group, flag,
  `HARDCHARGER_*` variable, YAML key, type, default and description, as JSON. The
  documentation's configuration reference is generated from it, so it can't drift from the
  server (`test_server_cli.py` checks it lists exactly what `--help` lists).

- `hardcharger --result-partitions N` (`HARDCHARGER_RESULT_PARTITIONS`): the command-line
  client reads a large result over up to N connections at once, in the same order.
- Partitioned ingest (Warp SQL §8.3, capability `partitioned_ingest`): one bulk load sent
  over several connections at once and inserted from all of them in parallel, in one
  transaction, all or nothing. Access control checks it like any ingest, and every part is
  bound to its session. Warp SQL's drivers use it for large uploads by default.
- `session_ended` errors carry their `reason` (Warp SQL §5.6): `killed` (KILL SESSION),
  `principal_dropped` (the user was dropped or disabled), `token_revoked` (a scoped token
  revoked), `token_expired` (a scoped token expired), `credentials_changed` (anything else
  access control revoked).
- DuckDB's httpfs, ducklake, postgres scanner, autocomplete and tpch extensions are built
  into the server, next to parquet, json and icu, at the commits DuckDB's own build of
  the pinned DuckDB uses. They work offline and in air-gapped deployments, with no
  extension repository (the pinned DuckDB is a pre-release, which the repository may not
  serve), and their libraries (libcurl, libpq, CRoaring) are linked in statically. `LOAD`
  of a built-in extension is a no-op; `INSTALL` is DuckDB's download, so offline it fails
  and is not needed.
- Each GitHub Release carries a `SHA256SUMS` file (`sha256sum -c` format) for its
  tarballs.
- `ghcr.io/gizmodata/hardcharger:main` and `:sha-<commit>`: the development image, pushed
  for each commit on `main` that passes every CI gate (ghcr.io only).

- Windows x64 and arm64 builds (MSVC): `hardcharger-windows-{amd64,arm64}.zip` (bin\
  with `hardcharger-server.exe` and the `hardcharger.exe` client, LICENSE,
  CHANGELOG.md) and an MSI of each that installs under Program Files and puts `bin\` on
  the PATH (unsigned for now). On Windows the console's Ctrl+C and Ctrl+Break stop the
  server as SIGINT and SIGTERM do elsewhere (drain first when `graceful_shutdown` is on, a second one stops at
  once); closing the console or shutting Windows down stops it at once. Outbound HTTPS
  (OpenID discovery, JWKS, OPA) trusts the Windows certificate store unless
  `SSL_CERT_FILE` names a bundle. The executables are single files: DuckDB and its
  built-in extensions, Arrow, OpenSSL, Boost, yaml-cpp, zstd, replxx and the C++ runtime
  (/MT) are linked in, so they import only Windows' own DLLs (CI checks the list) and
  need no Visual C++ Redistributable.
- The `hardcharger` client on Windows: Ctrl+C cancels the running statement (a console
  control handler hands it to a thread of the client's own), Ctrl+Break exits as SIGTERM
  does, colors and the pager use the console's ANSI mode, the password prompt doesn't
  echo, browser sign-in opens the default browser (or runs `HARDCHARGER_BROWSER`, a
  command line there, with the URL appended as its last argument, never through
  cmd.exe), and TLS trusts the Windows certificate store. Its cases at a terminal
  (pexpect, a pty) are marked `pty` and deselected on Windows, which has no pty.
- The sensitive-path guard on Windows: the account database and registry hives
  (`System32\config`), installer answer files, DPAPI keys, Credential Manager and the
  vaults, machine and user private keys, `%APPDATA%` credential files (gcloud,
  PostgreSQL's `pgpass.conf`, GnuPG), `_netrc`, the `\\.\` and `\\?\` device namespace,
  administrative shares (`\\host\C$`) and Kubernetes' `C:\var\run\secrets`, across
  Windows' spellings of a path (either separator, any case, `\\?\C:\`, trailing dots and
  spaces, `::$DATA` streams, 8.3 names, `file:` URLs).
- A DuckDB file as the access-control store, for development or a server that will only
  ever run one replica: `rbac_store_uri: duckdb:/path/to/rbac.duckdb` (or any path ending in
  `.duckdb`). PostgreSQL stays the store for production and for several replicas. The
  file (and its WAL) is created `0600`; an existing one other users can read or write is
  refused at startup (`chmod 600`), as is a store file that is also the database, the
  instrumentation or the log file. One server per file: a second one on the same file stops
  at startup, saying the DuckDB store serves a single server and to use PostgreSQL for
  several replicas. The file holds password hashes and grants, so no SQL reaches it, the
  administrator's included, however the path is written or computed (`'…/rb' || 'ac.duckdb'`,
  globs, `..`, relative, symlinks, hard links, `file:` URLs, case, `file_search_path`, `~`
  under another `home_directory`), its WAL included, through any reader (`read_blob`,
  `read_csv`, `read_json`, `read_duckdb`, ...), `ATTACH`, `COPY` (also its `tmp_` file renamed
  onto the target) and `EXPORT DATABASE`: a guard in the serving instance's DuckDB file
  system refuses every open, listing, move and removal of it, and a statement naming it is
  refused before it runs. Each change is checkpointed into the file at once: DuckDB skips a
  WAL it can't read, and every change in it (a REVOKE would come undone), so nothing is
  left to depend on the WAL. An extension with file I/O of its own (`sqlite_scanner`) goes
  past the file-system guard: for the store only a path written in the statement is refused (a
  computed one finds no SQLite database there), and the database, instrumentation and log
  files, which keep DuckDB's usual checkpoints, can lose their changes since the last one after
  a crash if the administrator writes a SQLite database where their WAL goes (README, Access
  control). Every access-control test file runs on both stores in CI.
  Tests: `test_rbac_duckdb_store.py` (124 on macOS, 134 on Windows with its 8.3 names,
  114 on Linux, which has no case-insensitive spelling to try).

### Changed

- `hardcharger` (the client) uses TLS by default, with a URI or without, as the drivers do
  (1.x's client, and Hardcharger's until now, were plaintext unless `--tls`). `--tls` is a
  boolean: `--tls=false` (or `HARDCHARGER_TLS=false`, or a URI's `?tls=false`) is plain HTTP;
  `--tls` on the command line wins over the variable, and a variable that isn't `true` or
  `false` keeps TLS on. A TLS handshake that meets a plain-HTTP server fails with "The server
  at HOST:PORT speaks plain HTTP, not TLS. Add --tls=false (or ?tls=false in a hardcharger://
  URI) to connect without encryption.": the client tells it from other TLS failures by sending
  one more ClientHello and seeing `HTTP/` where a ServerHello belongs, and never falls back to
  plaintext. The README, the migration guide, `--help`, the tarball smoke test and the image
  and Windows checks pass `--tls=false` to their plain-HTTP servers. Tests:
  `ClientTlsDefaultTest`; `test_client_cli.py` checks on the wire that the default sends a
  ClientHello, that against a plain server the client prints exactly that hint and sends only
  ClientHellos (no HTTP request, no password), and that `--tls=false`, `HARDCHARGER_TLS=false`
  and `?tls=false` connect.
- The refusal of a HUGEINT or UHUGEINT value decimal128(38, 0) can't hold says to run `SET
  SESSION arrow_lossless_conversion = true` (was "set the session option
  arrow_lossless_conversion to true"): a plain `SET` of that setting is DuckDB's `SET
  GLOBAL`, which only an administrator may run and which changes every session without a
  value of its own. The README and docs/migrating-from-gizmosql.md say so;
  `test_wide_integers.py` checks both scopes. (`scripts/patch-duckdb-arrow-hugeint.cmake`
  changed, so DuckDB is rebuilt.)

- `hardcharger` reads large results over up to 8 connections by default (was 1);
  `--result-partitions 1` reads over one.
- `mtls_ca_cert` without `tls_cert` and `tls_key` is refused at startup; it used to be
  ignored, so a server that seemed to require client certificates took plain HTTP. The other
  `mtls_*` options need `mtls_ca_cert`.

- `result_batch_rows` defaults to 122,880 (one DuckDB row group) instead of 2,048:
  SF10 lineitem over TLS 4.6 s -> 3.5 s, with zstd 26.4 s -> 6.3 s; small results are
  unchanged (one batch), a full scan's first batch takes 21 ms instead of 2 ms.
- Data transfer, from the Warp SQL SDK: TLS result streams encrypt whole gathered writes
  and overlap encrypting, encoding and producing (a TLS stream went from about a third
  of plaintext to the same speed on one AWS connection); uploads are read in 1 MiB pieces (plaintext
  ingest was slower than TLS); bulk ingest inserts on many DuckDB threads in one
  transaction, keeping the upload's row order. See docs/benchmarks.md.
- The server no longer installs or loads DuckDB extensions by itself
  (`autoinstall_known_extensions` and `autoload_known_extensions` are off): the ones it
  needs are built in, and any other takes an explicit `INSTALL` and `LOAD`.
- The Linux release tarballs (and the container image made from them) are built in
  manylinux_2_28 for a glibc 2.28 baseline, with OpenSSL 3 linked in statically: they run
  on any Linux distribution with glibc 2.28 or newer (Oracle/RHEL/Rocky/Alma Linux 8,
  Debian 10+, Ubuntu 20.04+, Amazon Linux 2023), not only on Ubuntu 24.04. CI checks the
  highest glibc symbol version of every shipped file and runs each tarball on Oracle
  Linux 8.

### Fixed

- Security: `hardcharger` (the client) connected in plaintext to a URI that asked for TLS. Its
  URI parser took 1.x's parameters (`useEncryption`, `username`, ...), ignored any other, and
  used plain HTTP unless `useEncryption=true`, so `hardcharger://host?tls=true`, and every
  `hardcharger://` URI without `useEncryption`, sent the sign-in (password included) as plain
  HTTP. The client's URIs now follow the Hardcharger ADBC driver's exactly:
  `hardcharger://HOST[:PORT][/PREFIX][?tls=false]` is HTTPS unless it says `?tls=false` (or
  `tls=0`), to port 31337 unless it names one, under `/warp` unless it names a path; `warp://`
  (port 443), `https://` and `http://` are accepted as the ADBC driver accepts them. `tls` is the
  only parameter: `useEncryption`, `username`, `tlsRoots`, `disableCertificateVerification`,
  `authType` and any other are refused by name (the message names the flag that does it now),
  and so are credentials in the URI, `hardcharger+http://` and bad ports. With a URI, the user
  and the other TLS and sign-in settings come from `--username`, `-W`, `--tls-roots`,
  `--tls-skip-verify`, `--mtls-cert`, `--mtls-key` and `--auth-type`; only `--host`, `--port`
  and `--tls` conflict with it, and `HARDCHARGER_HOST`, `HARDCHARGER_PORT` and `HARDCHARGER_TLS`
  no longer change what a URI names (`HARDCHARGER_HOST` replaced `hardcharger://localhost`'s
  host). `.connect URI [USERNAME [PASSWORD]]` takes the same URIs, and `.show` prints one.
  Tests: every
  form, in `client_config_test.cc` (`ClientConnectionUriTest`, 7 cases); end to end in
  `test_client_cli.py`, including that `?tls=true` (or no `tls` at all) to a listener puts a TLS
  ClientHello on the wire and never an HTTP request or the password, which the old client did.
- Preparing a query no longer runs it (warp-sql: the DuckDB backend learned a prepared
  SELECT's result schema by executing it, and now and then the dropped execution waited for
  the whole query: a JDBC, ADBC, JS or CLI prepare of an expensive query could hang for
  minutes). The schema now comes from binding the statement; access control lets the
  backend's `warp_result_schema()` through (it converts only the statement's own columns),
  so store users' prepares still describe their results. Tests:
  `test_a_prepare_describes_its_result_without_running_it` (enforcement) and
  `test_a_masked_columns_described_type_is_the_one_sent` (policies).
  `test_graceful_shutdown.py`'s in-flight prepared statement prepares the long query itself
  again (it passed the row count as a parameter so that the prepare stayed short).
- A prepared `INSERT`/`UPDATE`/`DELETE`/`MERGE` with `RETURNING` describes its rows'
  schema too (warp-sql 088d03e: bound and converted, the write never executed); it
  answered rows with no schema.
- `hardcharger`: a client certificate the server's handshake refused (revoked, or none
  sent where one is required) was reported as "Could not connect to ... is the server
  running?". It now says "The server at https://... refused the client certificate:
  certificate revoked", or that the server requires a client certificate (from warp-sql's
  client library, which reports these TLS alerts as Warp SQL §4.4's `certificate_revoked`
  and `client_certificate_required`).

- `hardcharger`: "OK, 1 rows affected" is "OK, 1 row affected".

- The session option `arrow_lossless_conversion` reported `false` after SQL had turned it
  on (`SET SESSION arrow_lossless_conversion = true`, or an administrator's plain `SET`);
  it reports what the session's results use (from the Warp SQL SDK).

- The answer to an ingest part or one-request upload sent again (same `Idempotency-Key`)
  reaches the client on Linux and macOS: the server answers it from the first upload without
  reading the body, and then reset the connection with the body unread, so the client saw a
  broken pipe or a reset and sent it again (warp-sql-adbc's `TestIntegrationIngestPartAnswerLost`
  counted a part sent three times). The connection now closes lingering, reading and dropping
  what the client still sends (up to 2 s, 256 MiB), as it already did on Windows. No row was
  ever written twice. From warp-sql `2dbcd84`; its conformance tests
  `test_the_answer_to_a_part_sent_again_arrives` and `test_the_answer_to_an_upload_sent_again_arrives`.

- A query that failed within about 5 s, after it had produced some rows, could be answered
  `200` with a `failed` statement document instead of its error (warp-sql: the SDK did so
  for a statement with buffered rows, and a request for partitions, which every driver
  sends by default, waits up to 5 s for the split decision). Every client shipped treats
  such a document as an error (Go ADBC, JDBC, JS, the C++ client), so no failure was
  reported as a success, but the answer was not the spec's. It is now the error response
  (`500 execution_error`), on submission and on prepared execution. That includes a
  HUGEINT/UHUGEINT value too wide for decimal128(38, 0), whose refusal came back as `201`
  with a `failed` document (`test_wide_integers.py` now asserts the error response).
  `server.yml`'s `failure_repro` dispatch input runs those conformance cases 200 times
  against both servers on every platform.
- The release binaries are self-contained, as GizmoSQL's: DuckDB (with its built-in
  extensions), OpenSSL 3, Arrow, Boost, yaml-cpp and replxx are linked in statically, so
  the tarballs hold just `bin/hardcharger-server`, `bin/hardcharger`, the licence and the
  changelog (no `lib/libduckdb`, no rpath). On Linux they need only glibc and the GCC
  runtime (0.2.0's needed the system's libssl.so.3); on macOS only the OS's own libraries
  and frameworks (0.2.0's linked Homebrew's openssl@3). CI fails a build that links
  anything else, and runs each tarball on a clean system.
- The access-control Open Policy Agent client verifies HTTPS against the system's CA
  bundle, as the server's other outbound calls do, rather than OpenSSL's compiled-in
  certificate directory (a build-time path in a portable build).
- A forced stop (a second stop signal during a drain) now and then aborted the server
  (SIGABRT) instead of exiting 0: the grace-period watcher read the drain's start time by
  reference from the signal thread's stack, after that thread had stopped the server and
  returned. AddressSanitizer caught it as a read of the freed stack frame (CI's ASan job,
  intermittently); a release build read whatever was left there. The watcher keeps its own
  copy.
- A stop (a forced one, or the first signal with `graceful_shutdown` off) during a bulk
  ingest whose body was still arriving never finished: the server waited for the engine
  thread, which waited for bytes from the connection the stop had already shut. Closing a
  session (a stop closes them all) now aborts its uploads, and the load rolls back.
- The graceful-shutdown tests show the end of the server's log when it exits non-zero or
  hangs, and cover a forced stop with a stalled result stream, an ingest upload (one
  request, or a part of a partitioned one), and prepared statements in flight, with
  `graceful_shutdown` on and off, and all of them at once over TLS with a client
  certificate.

- Security: `CREATE VIEW` (and `CREATE OR REPLACE VIEW`, temporary views, views created
  `WITH (defer_binding = true)`) needed only CREATE VIEW on the schema, so a user could
  create a view over a table or columns it can't read; the view failed only when read, but
  its creator owns it, and since metadata follows privileges it could list the view's
  columns, and so the column names of a table it holds nothing on. A view's query is now
  checked when the view is created, as the creator's own SELECT of it would be: SELECT on
  every table and view it reads and on each column it reads of them, through CTEs,
  subqueries, set operations and table macros, views it reads with their owners' rights,
  under SET ROLE, scoped tokens and `catalog_access` rules (also through a view over a
  denied catalog). The refusal says why with the SELECT's own reason ("user bob may create
  a view only over what it can SELECT itself, and the query of view c.main.v reads more:
  user bob has no SELECT privilege on table c.main.secrets"), so it tells no more than that
  SELECT would. Views still read with their owner's rights, so one granted to others keeps
  working, and a later REVOKE still closes it. Administrators, and servers without access
  control, are unchanged. `test_rbac_views.py` expects the refusal (see BATTERY.md and
  docs/migrating-from-gizmosql.md).
  `test_rbac_view_creation.py`, and a `catalog_access` case in `test_catalog_access_admin.py`.
- A view created by a `catalog_access` principal (a token on a rules-only server, or an
  administrator's token with rules under access control) could not be read by anyone, its
  creator included ("its owner is disabled or was dropped"): its owner was recorded as a
  store user that doesn't exist. Such a principal's rights are its token's rules, for its
  own sessions only, so what it creates now gets no owner (and clears any record an earlier
  object of that name left; dropping an object clears it too): its views read with each
  reader's own rights, the creator's rules included, and are refused when those don't reach
  what the view reads. `test_catalog_access_admin.py` (5 cases).
- Security: the server's own DuckDB files were readable and writable through SQL. An
  administrator, and under access control any user with `READ FILESYSTEM`, could read the
  instrumentation file (every user's statement text), the log file and the database file
  itself (every table, whatever the grants) with `read_blob()` and the other readers, and
  with `WRITE FILESYSTEM` (or as the administrator) write their `.wal`, which DuckDB replays
  into them at the next start. The instrumentation and log catalogs stay the only way in
  (administrators only, read-only), and the database is read through its catalog, under
  grants: the same file-system guard as the DuckDB store's lets only the instance's own
  storage open these files, under the path it attached them by.
- Security: metadata follows authorization object by object (Warp SQL §9.4). Inside a
  catalog a user could see, the metadata calls and SQL's catalog listings showed every
  schema, table, view (with its definition) and column, whatever the user's privileges;
  only other tenants' catalogs were hidden. Now the db-schemas, tables (and their `table_schema`), primary-keys,
  imported-/exported-keys and cross-reference calls, and `duckdb_tables()`,
  `duckdb_views()`, `duckdb_columns()`, `duckdb_schemas()`, `duckdb_constraints()`,
  `duckdb_indexes()`, `information_schema`, `SHOW TABLES`, `SHOW ALL TABLES`,
  `pragma_table_info()` and `pragma_show()` list a table or view only when the user holds
  SELECT on it or on any of its columns, or INSERT, UPDATE or DELETE on it (owning it
  holds them all); a column only when the user may SELECT it (or holds INSERT, UPDATE or
  DELETE on its table); a schema only when the user holds a grant on or in it or owns
  something in it; keys only when every table and column they name is listed. The rules
  ask the same `Check()` a SELECT does, so scoped tokens, SET ROLE, tenants and
  `catalog_access` rules (which grant whole catalogs) hold. `DESCRIBE`, `SHOW <table>`,
  `pragma_table_info()` and `pragma_show()` of a table the user holds no privilege on are
  refused ("user joe holds no privilege on table c.s.t, so it cannot describe it"), and
  `DESCRIBE` of a table the user may read only some columns of is refused too (it shows
  them all; `pragma_table_info()` lists the readable ones). DuckDB's own views (catalog
  `system`) and the session's temporary objects stay listed. A grant or revoke applies to
  the next listing, prepared ones included. `test_metadata_privileges.py`.
- HUGEINT and UHUGEINT values that Arrow's decimal128(38, 0) can't hold were sent wrong:
  DuckDB copies the 16 bytes as they are, so a UHUGEINT of 2^127 or more arrived negative
  (its maximum as -1) and any value of 10^38 or more as an invalid decimal128(38, 0).
  DuckDB is now patched (`scripts/patch-duckdb-arrow-hugeint.cmake`) to fail such a
  statement with a conversion error naming the value and the two ways around it (CAST to
  VARCHAR, or the `arrow_lossless_conversion` session option, DuckDB's 16-byte types,
  which hold every value); every value that fits is sent exactly as before, nested ones
  included. `test_wide_integers.py` round-trips every boundary value.
- JSON bind parameters outside the 64-bit range were bound as rounded doubles
  (10^38 - 1 as 1e38); they now bind exactly as HUGEINT or UHUGEINT, and wider ones are
  refused (from the Warp SQL SDK; Warp SQL §6.1 now requires it).
- The `arrow_lossless_conversion` session option changed the instance-wide default, so
  any user switching it changed every other session's HUGEINT, UUID and JSON results; it
  is now the session's own (from the Warp SQL SDK).
- `GET /warp/v1/sessions/{id}` of a session that `KILL SESSION` or access control ended
  answers `401` `session_ended` with its `reason` (Warp SQL §5.6), as every other request
  for it does; it answered `200`, so a liveness probe saw the dead session as alive. (The
  structured `reason` field itself is on main since after 0.2.0, which sent the reason
  only in the detail text.)
- Security: a `SET VARIABLE v = (SELECT ... FROM c.t)` whose subquery read a catalog the
  session's `catalog_access` rules deny was not checked, so `getvariable('v')` then handed
  back the result. Any rules principal was affected: a rules-only user, a restricted
  administrator, an RBAC administrator with rules. The value query is now plan-checked
  against the rules like any read. (GizmoSQL 1.x was not affected: its `catalog_access`
  check reads DuckDB's `StatementProperties.read_databases`, which already includes the
  subquery's catalog.) The same plan check already covered the other embedded-query forms
  (CTAS, INSERT ... SELECT, CTEs, scalar subqueries, EXISTS, COPY (SELECT ...), CREATE VIEW,
  DELETE/UPDATE with a subquery); each has a regression test.
- Security: a rules-restricted administrator could read the instrumentation and log
  catalogs, exposing every session's `sql_text` (with literals from catalogs its rules
  deny), and saw every session in `hardcharger_sessions()`. A restricted administrator now
  does not read those catalogs at all (hidden, as for a non-administrator) and sees only its
  own session; `KILL SESSION` needs an administrator without rules. An administrator without
  rules is unchanged (reads the catalogs, sees and kills every session). This is stricter
  than 1.x, where an administrator's token kept that access whatever its rules.
- Security: an administrator's token (`role: admin`) that carries `catalog_access` rules was
  not restricted by them; GizmoSQL 1.x applies a token's rules whatever its role. It now is,
  without and with RBAC: reads and writes per catalog (temp included) and what it sees
  (SHOW DATABASES, `information_schema`, `duckdb_*()`, the `_hardcharger_system` views,
  metadata requests), with 1.x's refusals ("Access denied: You do not have write access to
  catalog ..."). An administrator without rules (the password administrator, or a token
  without the claim) is unrestricted, as before. Like 1.x, a restricted administrator still
  reads the instrumentation and log catalogs (never writes them) and keeps SET GLOBAL,
  CHECKPOINT, KILL SESSION and every session in `hardcharger_sessions()`. Unlike 1.x, on
  purpose, it holds no privilege that reaches data around its rules: no ATTACH/DETACH (it
  could detach a denied catalog and attach its file under a name the rules allow), file
  reads and writes, INSTALL/LOAD, secrets or EXPORT/IMPORT DATABASE; no macros or types;
  only the table functions other rules principals get; sequences by the rules; and under
  RBAC it runs no access-control statements (CREATE USER, GRANT, ...). docs/migrating-from-gizmosql.md
  lists the differences.
- Security: a `catalog_access` claim that is present but isn't a list (a string, an object,
  `null`) was ignored, so the token kept full access (non-administrators: everything but the
  server's own catalogs). It now grants nothing, as an empty list does (1.x treats both as
  no rules).
- `hardcharger` (the client): a script whose last line is a comment after the final statement
  failed with "no SQL statement given" (exit 1). Comments after a statement, on lines of their
  own and before a statement (`--` and `/* */`) are no-ops in `-c`, `-f`, a pipe and a heredoc,
  as is a statement of comments alone (`/* note */;`).
- `hardcharger`: errors, the prompt and `.tables` were colored into pipes and files, and
  `NO_COLOR` was ignored. Color goes only to a terminal, never with `NO_COLOR` set to a
  non-empty value (https://no-color.org), line-editor hints and highlighting included.
  (GizmoSQL's `gizmosql_client` had no `--color` option, so there is none here either.)
- `hardcharger`: `.help` ran long command names into their descriptions
  (`.connect URI | HOST PORT USERConnect ...`). The description column starts after the
  longest command, and descriptions wrap at the terminal's width (80 into a pipe or file).
- `hardcharger`: a refused sign-in showed a generic message. It shows the server's problem code
  and detail, e.g. "Authentication failed for http://localhost:31337: invalid_credentials (the
  user name or password is not correct)", and still exits 1.
- `hardcharger_settings()` listed `max_queued_statements` with default `0`; the option's
  default is `-1` (8 x `max_concurrent_statements`). Every setting's default now comes from
  the configuration's own defaults, so on a server started without options each live value
  is its reported default. `max_queued_statements` reports the value as set (`-1` included),
  `SET GLOBAL` accepts `-1`, `-1` follows a later `SET GLOBAL max_concurrent_statements`, and
  `RESET` returns it (and `max_queue_wait`) to the startup value instead of a fixed `0`/`300`.
- `HARDCHARGER_VERSION()`, `hardcharger.version` in `hardcharger_settings()`, the
  `hardcharger_build_info` metric's `version` label and the instrumentation `instances` table
  said `v0.2.0` while `--version` printed `0.2.0`. All say `0.2.0` (the CHANGELOG's semantic
  version); `/warp` discovery and `server-info` already did (`0.2.0 (DuckDB ...)`).
- The `hardcharger_build_info` metric's help text still mentioned an edition. Hardcharger has
  none; no other user-visible server string mentions editions or licenses.
- The server's table functions (`hardcharger_settings()`, `hardcharger_sessions()`, ...) in a
  prepared statement returned no rows from the second execution on, and every execution showed
  the rows of the moment it was prepared: the rows were built when the statement was bound.
  They are now read again for each execution.
- The sensitive-path guard missed `file:/path` (one slash), which DuckDB opens as `/path`:
  `read_text('file:/etc/passwd')` read the file. Every file URL form DuckDB accepts is now
  checked as the path it opens.
- warp-sql 3aefd27: a request that reached a session just as the idle sweep closed it no
  longer runs in the closed session (its streamed result was cut mid-body: a Windows
  conformance run's `incomplete chunked read`). The request now keeps the session or gets
  `404 session_not_found`.
- Flaky tests, each named, counted and fixed rather than re-run (CI, 2026-10-16 to 10-18):
  `test_queued_execution_records_enqueue_time` (2, TSan) and
  `test_expired_queued_write_never_executes_after_slot_release` held the queue's slots for a
  fixed 1.5 s / 2.5 s, which a slow build's setup could outlast (both holders never active
  at once, or the probe never queued: "nothing recorded ... status = 'queued'"); the slots
  are now held until the test releases them. `test_no_answer_means_no` (3: `[not_a_bool]` on TSan,
  `[error500]` and `[not_a_bool]` on Windows amd64): a healthy exchange with the mock
  policy service sometimes outlasted the test server's 800 ms deadline, so a session was
  refused before the service misbehaved. The deadline is now 5 s (stretched on sanitizer
  builds), and the "slow" service answers only when released. A CI run measured the
  healthy exchange at under 150 ms on every platform (Windows p99 149 ms, TSan 95 ms), so
  the failures were stalls, not steady latency: a refusal of a healthy service now reports
  the service's latest requests (arrived or not, and how long each took). The TSan job's data race in
  `ExtensionCallbackManager::Register` / `Optimizer::Optimize` (2) came from `LOAD spatial`
  (a prebuilt, uninstrumented download) while another thread planned a query:
  `test_geoarrow.py` now loads it in the init SQL, before anything else runs
  (`server/tests/tsan.supp` explains why it is not suppressed). A TSan race in libpq's
  `pqSaveParameterStatus` (two threads opening PostgreSQL connections at once write the
  process-wide statics that only the deprecated `PQescapeString`/`PQescapeBytea` read,
  which nothing here calls) is suppressed by that function name, with the source lines.
- `test_client_shell.py` (1, linux-arm64 on cadc02e): the script's `run_test` checked with
  `echo "$actual" | grep -qF` under `set -o pipefail`. `grep -q` exits at its first match, and
  an `echo` still writing then dies of SIGPIPE, so the pipeline returned 141 and a match read
  as a failure ("Verify INSERT result", with `Frank` in the output it printed);
  `run_test_not_contains` could pass in the same way. Both now use a here-string. Reproduced
  with the same output: 1 in 20,000 under CPU load on Linux arm64, and nearly every time with
  output beyond a pipe's buffer; never with the here-string.

### Documentation

- README: the JavaScript example uses `@gizmodata/hardcharger` (gizmodata/hardcharger-js:
  `import { connect } from "@gizmodata/hardcharger"`, `hardcharger://` URLs), the Hardcharger
  client on `@gizmodata/warp-sql`, as the Python example uses `adbc-driver-hardcharger`.
- Access control is described as Hardcharger's own (GizmoSQL 1.x has none);
  docs/migrating-from-gizmosql.md maps no 1.x option to `--rbac-*`, `--token-groups-claim`
  or `--audit-chain-key`.
- README: hardcharger-adbc is a native Go driver whose library every ADBC driver manager loads
  as `driver = "hardcharger"` with `hardcharger://` URIs.
- README: the client examples use the Hardcharger drivers, `adbc-driver-hardcharger`
  (`from adbc_driver_hardcharger import dbapi as hardcharger`, `hardcharger://` URIs) and
  `hardcharger-jdbc` (`jdbc:hardcharger://`); a Python quick start with `pip install hardcharger`
  (the server) beside `pip install adbc-driver-hardcharger` (the client); badges for both.

## [0.2.0] - 2026-10-17

Role-based access control and GizmoSQL 1.x's server features (instrumentation, catalog
logging, catalog access rules, statement queue, metrics, KILL SESSION, the OAuth server,
JWKS tokens and more), checked against 404 of the 423 server-side cases of GizmoSQL's
test battery, plus two security reviews of the result (see Fixed).

### Added

- Role-based access control: `CREATE USER/ROLE`,
  `GRANT`/`REVOKE` (roles, table, view, column, schema and catalog privileges, `WITH GRANT OPTION`, `WITH ADMIN OPTION`, cascading revokes),
  `SHOW GRANTS`, ownership (`ALTER ... OWNER TO`), views that read with their owner's
  rights, system privileges (`READ FILESYSTEM`, `MANAGE CATALOGS`, ...), row and masking
  policies, tenants, `SET ROLE`, identity-provider group mappings, scoped tokens and an
  Open Policy Agent service. Every statement is checked inside DuckDB against its own
  plan, when it is bound and again before it is optimized; anything the check cannot
  classify is refused. Turned on by `rbac_store_uri` (a PostgreSQL database every replica
  shares, reached through DuckDB's `postgres` extension); `rbac_opa_url`,
  `rbac_opa_timeout_ms` and `token_groups_claim` too.
- Store users sign in with their password; their access tokens are checked against the
  store on every request, so dropping or disabling a user, changing their password or
  revoking a scoped token ends their sessions and tokens at once, on every replica, and a
  revoke stops a statement that is running.
- Statements that reach outside the database (files and URLs, `ATTACH`, extensions,
  secrets, instance-wide settings, `query()`, table functions not on the allow-list) are
  refused for store users before DuckDB binds them, unless they hold the system privilege
  that opens them. Settings that write files (`log_query_path`, `profiling_output`) need
  WRITE FILESYSTEM, and settings that hold credentials (`http_proxy_password`, ...) need
  MANAGE CONFIG to read.
- A tenant's user is told about another tenant's objects exactly what it is told about
  missing ones, in errors, metadata, `DESCRIBE`, `CREATE VIEW` and session options.
- Access-control audit records form a tamper-evident hash chain (HMAC-SHA256 with
  `audit_chain_key`, SHA-256 without); `hardcharger
  --verify-audit-log FILE` checks a JSON log.
- DuckDB is built with a small binder patch (`server/scripts/patch-duckdb.cmake`) that
  lets access control see which part of a plan came from which view.
- Tests for access control (`server/tests/test_rbac_*.py`, 262), including two replicas on
  one store and a gate that access control costs no concurrency. They need PostgreSQL.
- SQL functions for the session's context: `HARDCHARGER_USER()`, `_ROLE()`, `_VERSION()`,
  `_CURRENT_SESSION()` (a UUID), `_CURRENT_INSTANCE()`, `_CURRENT_CLUSTER()`,
  `_INSTRUMENTATION_ENABLED()`, `_INSTRUMENTATION_CATALOG()`, `_INSTRUMENTATION_SCHEMA()`;
  table functions `hardcharger_settings()`, `hardcharger_sessions()` and
  `hardcharger_metrics()`.
- Server settings in SQL: `SET [SESSION|GLOBAL] hardcharger.<name>` and `RESET`, with
  1.x's scopes: `query_timeout`, `query_log_level`, `graceful_shutdown`,
  `shutdown_grace_period_seconds`, the statement queue's limits, `bypass_queue`, the
  session and query tags; startup settings say where to change them. `SET GLOBAL` needs
  an admin.
- `KILL SESSION '<uuid>'`: ends another session at once,
  frees its `max_sessions` place, and its next request gets `session_ended`.
- `max_sessions` (admins are never capped; `too_many_sessions`), `session_idle_timeout`.
- The admin command guard (servers without access control): users without the admin role can't ATTACH/DETACH, INSTALL or
  LOAD, SET GLOBAL (or instance-wide settings), CHECKPOINT, COPY to or from local files,
  read local files (`read_csv('/x')`, `FROM 'x.csv'`), EXPORT/IMPORT DATABASE, CONNECT,
  manage secrets, or use `quack_*()`; only literal remote URLs pass where a path decides.
- The sensitive path guard (`block_sensitive_paths`, `sensitive_paths`): no statement may
  name credential or system files (`~/.ssh`, `/etc/shadow`, the server's TLS key, ...), however spelled; `secret_directory` can't change after startup. And
  `allow_unredacted_secrets` can't be turned on (`block_unredacted_secrets`); SQL with a
  NUL byte is refused.
- `_hardcharger_system`, a read-only catalog with `hardcharger_index_info` and
  `hardcharger_view_definition`.
- The statement queue: `max_concurrent_statements`, `max_queued_statements`,
  `max_queue_wait`, `admin_bypass_queue_default`.
- Metrics: `enable_metrics`, `GET /metrics` on `metrics_port` (`hardcharger_*` metrics),
  `hardcharger_metrics()`, the unclean-exit marker.
- The health check runs `health_check_query` in the background every
  `health_check_interval_seconds`; a probe answers from the last outcome, which goes stale
  after `health_check_staleness_seconds`.
- Graceful shutdown: `graceful_shutdown` (off: the first signal stops at once) and
  `shutdown_grace_period_seconds` (0: wait forever), both adjustable live with
  `SET GLOBAL`; a second signal stops at once.
- `token_authorized_emails`: which users a sign-in token may name.
- `auth_log_level`, `query_log_level`, `session_log_level`, `log_file`; a bearer token's
  first use logs at INFO, its repeats at DEBUG; JSON lines carry `instance_id` and
  `cluster_id`.
- `cluster_id`, `max_header_bytes` (what 1.x's `--max-metadata-size` capped),
  `result_batch_rows` (default 2048, as 1.x streamed).
- Instrumentation: `enable_instrumentation` records instances, sessions, statements and
  executions (with their queue wait, fetch timeline and status) in `_hardcharger_instr`,
  with 1.x's tables, views and columns (the version column is `hardcharger_version`), in a DuckDB
  file next to the database (`instrumentation_db_path`) or in a DuckLake or PostgreSQL
  catalog the init SQL attaches (`instrumentation_catalog`, `instrumentation_schema`). A
  killed server's open rows are closed at the next start. Queued statements show as `queued` while they wait.
- `instance_tag`, `SET hardcharger.session_tag` and `SET hardcharger.query_tag`, recorded with
  the instance, the session and each statement; `capture_query_profile` (`off`,
  `standard`, `detailed`; also `SET [GLOBAL]`) stores DuckDB's JSON query profile with
  each execution.
- Catalog logging: `enable_catalog_logging` also writes every log line to
  `_hardcharger_logs.logs` (1.x's columns), in its own file
  (`log_catalog_db_path`) or a DuckLake or PostgreSQL catalog (`log_catalog`,
  `log_schema`), in batches from a background writer.
- The instrumentation and log catalogs are the server's: admins may read them, nobody may
  write, detach or `USE` them, and non-admins don't see them in metadata.
- Identity provider sign-in: ID tokens verified with the
  provider's JWKS (`token_jwks_uri`, or the one `token_allowed_issuer`'s OpenID
  configuration names), RS256/384/512 and ES256/384/512, through `token_exchange` or as
  the password of user `token`.
- Browser sign-in: `oauth_client_id` runs GizmoSQL 1.x's OAuth server on `oauth_port`
  (default 31339) with its routes (`/oauth/initiate`,
  `/oauth/start`, `/oauth/callback`, `/oauth/token/<uuid>`), as a confidential client
  (`oauth_client_secret`, `oauth_scopes`, `oauth_base_url`, `oauth_redirect_uri`,
  `oauth_instance_id` for a shared callback proxy, `oauth_disable_tls` behind a TLS proxy).
- `--verify-tls URL`: fetches a URL with the system's CA bundle (`SSL_CERT_FILE`, else the
  first well-known bundle), as outbound HTTPS does, and exits 0 or 1.
- The GizmoSQL 1.x test battery, ported (`server/tests`, scoreboard in
  `server/tests/BATTERY.md`), with C++ unit tests (`server/unit`, googletest 1.18.0), on
  the one build that ships.
- Per-catalog permissions: a sign-in token's
  `catalog_access` rules (`read`, `write`, `none` per catalog name or glob, first match
  wins, no match is no access) are enforced on every statement's plan by access control,
  without a store: reads and writes per catalog (temp included), and what the user sees
  in SHOW DATABASES, `information_schema`, `duckdb_*()`, the `_hardcharger_system` views and
  the metadata requests; refusals read as in GizmoSQL 1.x ("Access denied: You do not have
  write access to catalog ..."). Under RBAC the rules grant nothing.
- The instrumentation and log catalogs are now also left out of SQL's own catalog
  functions (`duckdb_databases()`, `information_schema`, SHOW ...) for non-administrators.
- Instrumentation writes to a shared DuckLake catalog that lose a commit race to another
  server are tried again (a few times, backing off) instead of being dropped.
- `PIVOT` without an `IN` list runs (it expands into several statements), also for
  read-only principals.
- Access control and the battery's features work together: store users never see or
  write the instrumentation and log catalogs (admins read them, nobody writes them), may
  call the server's SQL functions (`hardcharger_settings()`, ...), and are refused files,
  URLs, ATTACH and the like by access control's system privileges rather than by the
  admin command guard, which stays for servers without access control. Access-control
  statements are counted, logged and recorded like other statements
  (`test_rbac_battery.py`).

### Changed

- The server's executable is `hardcharger-server` (the Docker image's entrypoint too); the
  plain `hardcharger` name is kept for a command-line client.
- No license: every feature that was Enterprise in GizmoSQL 1.x is available to everyone,
  each turned on by its own option. The license options (`license_key`,
  `license_key_file`) and their environment variables are gone: a license flag is an
  unknown option like any other. There is one build, and it has everything: no
  test-license build, no Enterprise/Core build switch, no edition (`EDITION()`, the
  `edition` setting, server-info and metrics label, and the instances table's
  `gizmosql_edition` column are gone).
- Hardcharger takes no GizmoSQL 1.x names: no `GIZMOSQL_*` environment variables, no 1.x
  flags (`--readonly`, `-D`, `--tls CERT KEY`, ...) or unprefixed environment names
  (`TLS_CERT`, `SECRET_KEY`, `INIT_SQL_COMMANDS`, ...), no `SET gizmosql.*`, no
  `GIZMOSQL_*()` or `gizmosql_*()` functions, no `_gizmosql_*` catalogs, no `gizmosql_*`
  metrics, no `GIZMOSQL:COLUMN_DEFAULT` metadata key,
  and no server tokens with `iss=gizmosql`. Every option is its `--flag`, its
  `HARDCHARGER_*` variable or its YAML key; leftover GizmoSQL variables are not read. The
  1.x-only options `backend` and `reject_unknown_sessions` are gone. See
  `docs/migrating-from-gizmosql.md` for the name mapping.
- The administrator's credentials are `--admin-username` / `--admin-password`
  (`HARDCHARGER_ADMIN_USERNAME` / `HARDCHARGER_ADMIN_PASSWORD`, YAML `admin_username` /
  `admin_password`); 0.1.x's `--username` / `--password` and `HARDCHARGER_USERNAME` /
  `HARDCHARGER_PASSWORD` are no longer read.
- Release images also go to Docker Hub (`gizmodata/hardcharger`), with the same tags as
  ghcr, signed in by OIDC (no stored token); the release refuses to push to, and fails
  after pushing to, a repository anyone can pull.

- `drain_seconds` is now `shutdown_grace_period_seconds` (default 300, as in 1.x).
- Table schemas from `metadata/tables?include_schema=true` carry nullability, column
  comments, auto-increment and defaults (`WARP:SQL:COLUMN_DEFAULT`).
- Warp SQL never recreates a session implicitly: an unknown session id is
  `session_not_found`.

### Fixed

- Security review of rules-only access control (catalog_access rules without an RBAC
  store): a non-administrator principal no longer holds every system privilege. It is
  held to a GizmoSQL 1.x non-administrator's limits, checked on each statement's own
  plan, so nested SQL is caught where it runs: no file or URL reads (also through
  `query('...')`), no secrets, no extensions, no table function outside the allow-list
  (`destroy_external_resource`, `pragma_storage_info`, ...), no server-wide or credential
  settings, and no reading a credential setting. Creating a macro or type (temporary
  ones included, except PIVOT's own enum) needs an administrator, since a macro's or
  type's body runs unchecked when it is used; nested SQL through `query()`,
  `query_table()` and `json_execute_serialized_sql()` is refused because those table
  functions are off the non-administrator allow-list. Sequence functions (`nextval`, `setval`,
  `currval`) need write (currval: read) access to their catalog. `DESCRIBE` of a catalog
  the user may not see is refused. A plain server (no store) now routes every non-admin
  token principal through the same plan check.
- Credential redaction: SQL comments
  (`CREATE /* */ SECRET`), string literals and dollar-quoting no longer hide the keyword,
  so `CREATE [OR REPLACE] [PERSISTENT|TEMP] SECRET`, `CREATE`/`ALTER USER` and
  `SET`/`PRAGMA` of a credential setting are redacted wherever they appear, with a
  raw-word backstop. DuckDB parse errors that echo such a statement are dropped, ATTACH
  connection strings and credential-setting values are masked (idempotently, the 1.x key
  list plus `x-amz-*` and `user:***@` URLs), and all of it reaches the log output,
  `_hardcharger_instr` (`sql_text`, `prepare_error`, `error_message`), the `_hardcharger_logs`
  catalog and the access-control audit records, which are redacted before they are sealed
  so the hash chain still verifies.
- Statement logs, instrumentation's `sql_statements` and the catalog logs no longer keep
  `CREATE SECRET` or `CREATE`/`ALTER USER` text, which can carry credentials.
- The server could crash while stopping: a thread finishing after OpenSSL's exit cleanup
  touched freed state. OpenSSL no longer frees its global state at exit.
- A NULL bound through an Arrow parameter keeps its Arrow column's type when DuckDB can't
  infer the parameter's (`SELECT ? + 1`); LIST and interval Arrow parameters bind (an
  interval with sub-microsecond precision is refused, not truncated); prepared
  statements describe DECIMAL, UUID, LIST and INTERVAL parameters.
- Opening a session with an unknown option says `unknown_option`.
- Metadata `type-info` (JDBC `getTypeInfo`, Flight SQL `GetXdbcTypeInfo`) answered 404; it
  lists DuckDB's types, and a tenant's users see all of them.
- Bulk ingest of a column with no values (Arrow null type) creates a VARCHAR column;
  half floats and nested day-time intervals are refused with the reason; dictionary-
  encoded parameters bind; listing catalogs no longer opens every attached catalog; a
  stop with a request in flight no longer crashes.
- Table schemas from metadata give PRECISION in decimal digits for every numeric type,
  column comments (REMARKS) and defaults (`WARP:SQL:COLUMN_DEFAULT`).
- (Warp SQL SDK) A server stopped with a request in flight no longer crashes;
  dictionary-encoded parameters bind; ingest turns null-typed columns into VARCHAR,
  converts day_time intervals and refuses types DuckDB can't store; parameter schemas
  carry DuckDB's own Arrow types (VARIANT, GEOMETRY and UUID extensions); listing
  catalogs no longer opens every attached catalog.
- The `hardcharger_*()` table functions no longer destroy a column type DuckDB only lent them.
- (Warp SQL SDK) Cancelling a statement just after submitting it no longer leaves it running:
  an interrupt that reached DuckDB before its query began was forgotten, so the cancel is
  now repeated until the statement ends.

### Known gaps

- Not yet ported from GizmoSQL 1.x: OpenTelemetry export.
- Browser sign-in has only 1.x's routes on `oauth_port`; Warp SQL's `oauth_browser` flow
  (`/warp/v1/auth/oauth/flows`) needs the Warp SQL SDK to offer it.
- Instrumentation's fetch times are when the server took each batch from DuckDB, not when
  the client received it (the Warp SQL SDK has no delivery callback); its `peer` column
  is empty (the SDK's principal carries no peer address).
- Listing the schemas or tables of one catalog still opens every attached catalog: the v2
  C API has no catalog-scoped listing.
- VARIANT (and other struct or extension) Arrow parameters can't be bound yet.
- `glob()` over a directory holding a protected file is refused, not filtered; a path an
  admin computes at run time is not checked (no file-system hook in the v2 C API).

## [0.1.2] - 2026-10-17

0.1.1 was tagged but never released: its release gate failed on a race in deleting a
running write (fixed here). 0.1.2 carries everything listed for it.

### Fixed

- A use-after-free: prepared statements kept parameter types that DuckDB had lent
  them, so closing a prepared statement or ending its session could crash the server or
  corrupt its heap (found by the JDBC driver's tests). Bulk ingest freed borrowed column
  types the same way. Both are fixed, and CI now runs the server tests and the
  conformance suite on an AddressSanitizer build that gates every release.
- Deleting a statement before the engine had said whether it writes could cancel a
  write; such a delete is now settled once it does (a write finishes, a read stops).
- A long poll on a finished statement answered only after its whole wait; it answers at
  once.
- A streamed query sent its Arrow schema only with its first batch, so an aggregate's
  response stayed silent until the query ended and proxies' idle timeouts could cut it;
  the schema now goes first.
- A client that hung up while a query's stream waited for its next batch went unnoticed
  until the query ended; the query is cancelled within a second. A statement that
  changes data (`INSERT … RETURNING`) is never undone because its reader left or deleted
  it: it runs to the end.
- Finished statements stayed in their session forever; they are forgotten 10 minutes
  after they finish (once any retained result has expired), or beyond 1,000 a session.
- Prepared statements describe their result (`result_schema`, `result_kind`) without
  running; executing a query for zero parameter rows returns no rows instead of a count.
- Table schemas from metadata report nullability, precision and scale; the session
  document follows SQL `USE`; a missing or existing table is SQLSTATE 42P01 / 42P07.

### Added

- `POST /warp/v1/sessions/{id}/cancel` stops whatever the session is running.
- Primary and imported keys of every table in one metadata request.

## [0.1.1] - 2026-10-17 [not released]

See 0.1.2.

## [0.1.0] - 2026-10-17

### Added

- The Hardcharger server (`server/`): DuckDB 2.0 behind the Warp SQL protocol, built on
  the Warp SQL server SDK (`third_party/warp-sql`). It passes the Warp SQL conformance
  suite.
- Configuration from flags, environment variables (`HARDCHARGER_*`, plus GizmoSQL 1.x's
  names, so GizmoData Cloud's settings work unchanged) and a YAML file
  (`--config`/`HARDCHARGER_CONFIG`), in that order of precedence; unknown keys are refused.
- Access tokens as JWTs signed with `secret_key` (HS256): valid across restarts and, with
  `allow_cross_instance_tokens`, across replicas. GizmoSQL 1.x-issued server tokens are
  accepted.
- Sign-in with RS256 tokens from a trusted issuer (`token_exchange`, or as the password of
  user `token` as in 1.x), with issuer and audience checks and the `admin`, `user` and
  `readonly` roles; `catalog_access` needs the `catalog_permissions` license feature.
- GizmoData Enterprise license keys, verified exactly as GizmoSQL 1.x does; a bad key
  stops the server.
- Init SQL (commands, then file; any failure stops the server), read-only databases,
  memory limit, storage version, unsigned extensions, a plaintext health port, a default
  statement timeout, statement logging, text or JSON logs, TLS and mTLS, graceful drain.
- Tests (`server/tests`, 27) for all of the above.
- Release artifacts: `hardcharger-linux-amd64`, `-linux-arm64` and `-macos-arm64`
  tarballs (server plus `libduckdb`), and the image `ghcr.io/gizmodata/hardcharger`.
- A benchmark derived from TPC-H, the 22 queries at SF10 (`bench/tpch.py`, results in
  `docs/benchmarks.md`): 1.44× faster than GizmoSQL 1.41 over the 22 queries, and 1.6× faster
  at moving 10M rows. Derived from the TPC-H benchmark; not an official TPC-H result and not
  comparable to published TPC-H results, as these results do not comply with the TPC-H
  specification. TPC-H™ is a trademark of the Transaction Processing Performance Council (TPC).

### Known gaps

- The `readonly` role allows only SELECT and EXPLAIN until the authorization module
  brings DuckDB's own read-only analysis.
- Not yet: access control (users, roles, grants). Not yet ported from GizmoSQL 1.x:
  instrumentation, metrics, the statement queue, KILL SESSION, OAuth sign-in, the admin
  and sensitive-path guards.
