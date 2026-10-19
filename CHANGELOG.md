# Changelog

All notable changes to the Hardcharger server are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The Python package keeps its
own changelog in `python/CHANGELOG.md`.

## [Unreleased]

### Added

- The session document says where the session's transaction stands (Warp SQL §5.2,
  warp-sql 6afe76d): `GET /warp/v1/sessions/{id}` and the session's creation answer carry
  `transaction`, `none`, `open` or `aborted`, as the engine has it, read after every
  statement, ingest, option change, commit and rollback. `test_rbac_set_role.py` checks it
  for a store user through an allowed, a refused and a failed statement; the conformance
  suite's ten new session tests pass on this server.

### Changed

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

### Security

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
- A TPC-H SF10 benchmark (`bench/tpch.py`, results in `docs/benchmarks.md`): 1.44× faster
  than GizmoSQL 1.41 over the 22 queries, and 1.6× faster at moving 10M rows.

### Known gaps

- The `readonly` role allows only SELECT and EXPLAIN until the authorization module
  brings DuckDB's own read-only analysis.
- Not yet: access control (users, roles, grants). Not yet ported from GizmoSQL 1.x:
  instrumentation, metrics, the statement queue, KILL SESSION, OAuth sign-in, the admin
  and sensitive-path guards.
