# Backpressure limits validation

Local macOS ARM64 Release build, 2026-10-26. Warp SQL commit 4472d61.

- Warp SQL `cpp/tests`: 88 SDK tests and 35 client tests passed. Includes oversized
  declared metadata/body/expanded lengths before allocation, dictionary compression,
  per-message reset, atomic global reservations, concurrent owners, nested/dictionary
  accounting, cancellation/destruction and expiry with an active read.
- Hardcharger `server/tests`: the full parallel run (including Docker service cases)
  reported 1977 passed, 7 expected failures, and 3 failures. One assertion needed to
  include the new `hardcharger.max_ingest_message_bytes` settings row. Two terminal
  tests inherited the runner's `NO_COLOR`/dumb terminal environment. After correcting
  that assertion and using `env -u NO_COLOR TERM=xterm-256color`, all three passed.
  All 5 serial tests passed. The 3 later-part upload-limit tests added after collection
  also passed, giving 1988 passing cases and 7 existing expected failures in total.
- All 21 ingest-limit integration cases passed, including later-part failure rolling
  back earlier accepted parts; all 11 retained-limit integration cases passed.
- Warp SQL conformance against Hardcharger: 283 passed, all 38 OpenAPI operations checked.
- Conformance against `warp-duckdb`: 281 passed, 2 expected authorization skips, all 38
  OpenAPI operations checked. The reference backend does not implement that authorization.
- Both repositories' `git diff --check` passed.

Build dependencies are the existing pinned DuckDB/Arrow artifacts from the release
checkout. Tests use temporary servers/data and the existing test PostgreSQL/S3 services.
The user's desktop database is not used by these tests.

The starvation test in the [resume design](backpressure-resume-design.md) is an acceptance
test proposal, not a claimed passing test. This patch does not implement Gap 1 or the
proposed global unretained-buffer budget.
