# The DuckDB extensions built into Hardcharger's libduckdb (build-deps.sh passes this file
# to warp-sql's build-duckdb.sh as WARPSQL_DUCKDB_EXTENSION_CONFIGS). Built in, they work
# with no network and no extension repository: DuckDB's repository may lack one for a
# platform or for a while (a probe on a DuckDB pre-release met "Extension Autoloading
# Error" for one), and air-gapped deployments have no repository at all. Any other
# extension still installs and loads on request (INSTALL, LOAD); the server never installs
# or loads one by itself (src/engine.cc).
#
# DuckDB also builds a loadable copy of each extension, which nothing here ships. Linked
# against libduckdb's symbols at load time rather than a static copy of DuckDB: smaller,
# faster to link, and on macOS the loadable autocomplete linked with a static DuckDB fails
# at this DuckDB commit (undefined DuckDB symbols).
set(EXTENSION_STATIC_BUILD FALSE)

# DuckDB always builds in core_functions, parquet, json and icu. A loaded extension is
# only built; duckdb_extension_statically_link() (at the end) puts it into libduckdb.
# In-tree:
duckdb_extension_load(autocomplete)
duckdb_extension_load(tpch)

# Out-of-tree, from their repositories at the commits DuckDB's own build of this DuckDB
# commit uses (<duckdb>/.github/config/extensions/<name>.cmake), with DuckDB's patches for
# them (<duckdb>/.github/patches/extensions/<name>/). build-deps.sh fails when DuckDB's
# pins no longer match these, so a DuckDB bump moves them on purpose. Their libraries
# come from build-extension-deps.sh (CRoaring, libpq: static) and the system (libcurl).
duckdb_extension_load(httpfs
    GIT_URL https://github.com/duckdb/duckdb-httpfs
    GIT_TAG 77392a72077a85897db2519fae11e2c0da972b8a
)
duckdb_extension_load(ducklake
    GIT_URL https://github.com/duckdb/ducklake
    GIT_TAG 417a9117f590e8141742ac0579d02f6a90162e37
    APPLY_PATCHES
)
duckdb_extension_load(postgres_scanner
    GIT_URL https://github.com/duckdb/duckdb-postgres
    GIT_TAG c0f5a3c0c4b68b02afbf669d36d7f0e503985c88
    SUBMODULES database-connector
)

duckdb_extension_statically_link(autocomplete tpch httpfs ducklake postgres_scanner)

# The postgres scanner links libpq into its loadable extension only; built in, its static
# library needs libpq too (static, from build-extension-deps.sh), or libduckdb has the
# PQ* symbols undefined. Deferred to the end of DuckDB's configure, once the target exists.
function(hardcharger_link_libpq)
  find_package(PostgreSQL REQUIRED)
  find_package(OpenSSL REQUIRED)
  target_link_libraries(postgres_scanner_extension PostgreSQL::PostgreSQL OpenSSL::SSL OpenSSL::Crypto)
endfunction()
cmake_language(DEFER DIRECTORY "${CMAKE_SOURCE_DIR}" CALL hardcharger_link_libpq)
