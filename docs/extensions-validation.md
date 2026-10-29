# Runtime extension validation

Local macOS ARM64 static Release build, 2026-10-26, preparing v2.1.0.
DuckDB v2.0.0 d54cbbe; Chargewire b74af00 (unchanged submodule pin).

- A fresh dependency build links only core_functions, parquet, json and icu.
- 18 targeted integration/build-cache checks passed: signed runtime modules; S3 and
  PostgreSQL reads/writes; DuckLake on PostgreSQL/S3; autocomplete and tpch; automatic
  installation/loading from an empty cache; offline loading of an installed module;
  FORCE INSTALL from a local repository serving a signed compatible module; retired
  patch/cache cleanup; upstream metadata upgrade behavior. Signature checks stay on.
- Full parallel server suite: 1,959 passed, 7 existing xfails, one outdated assertion
  expecting a built-in PostgreSQL scanner failed. Updated it for the runtime scanner
  and reran it successfully with both PostgreSQL and DuckDB stores. All five serial
  tests passed. This gives 1,965 passing cases plus seven existing xfails across the
  complete suite and the corrected rerun.
- Additional DuckDB-store access-control pass: 533 passed.
- Server C++: 168 passed, one existing PostgreSQL instrumentation skip. Client C++
  cases ran through the server suite’s fixtures (a bare invocation has no test server).
- Chargewire C++: 74 SDK and 35 client tests passed.
- Hardcharger protocol conformance: 283 passed; all 38 OpenAPI operations checked.
- Portable-library check: server/client link only macOS libraries/frameworks.
- Python/server versions agree at 2.1.0; release arithmetic and version-manifest tests
  passed; notices list only the four standard native built-ins and iOS’s optional set.
- Desktop staging rejects a mismatched native archive before running prepare. The
  rebuilt Mac app passes strict signature integrity and its bundled server explicitly
  retains the extension-loading entitlement. This local review app is ad hoc signed,
  not Developer ID notarized.

Windows and iOS build/signing acceptance remains a CI check; local results above do
not claim those platforms were built. The former DuckLake compatibility patch is gone.
Upstream currently rejects explicit META_STORAGE_VERSION with a duplicate-option error;
no compatibility workaround or older-reader guarantee is promised. Plain DuckDB storage
version settings continue to work. No user database was used by these test suites.
