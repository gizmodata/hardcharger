# The baseline Linux x86_64 build (HARDCHARGER_CPU_TARGET=baseline, issue #62): DuckDB's four
# default built-ins plus the extensions people load most, all compiled here for any x86-64
# CPU. DuckDB's downloadable extensions need x86-64-v3 (AVX2), so on an older CPU the server
# loads none of them (src/authz/extension_cpu_guard.cc); these are the ones that work there.
# On a CPU with AVX2 this build still installs and loads the others like the optimized one.
# Pins follow DuckDB's own extension configs, as in duckdb-extensions-ios.cmake, and are
# checked by deps-duckdb.sh.
set(EXTENSION_STATIC_BUILD FALSE)
set(DUCKDB_CAPABILITIES "httplib;loadable_extensions" CACHE STRING "" FORCE)

duckdb_extension_load(core_functions)
duckdb_extension_load(parquet)
duckdb_extension_load(json)
duckdb_extension_load(icu)

# Out-of-tree, from their repositories at the commits DuckDB's own build of this DuckDB
# commit uses (<duckdb>/.github/config/extensions/<name>.cmake), with DuckDB's patches for
# them. Their libraries come from build-extension-deps.sh (CRoaring, libpq, libcurl: static).
duckdb_extension_load(httpfs
    GIT_URL https://github.com/duckdb/duckdb-httpfs
    GIT_TAG f71cfdb4bff85b9e15b1f3b842d9aa395aff0167
)
duckdb_extension_load(ducklake
    GIT_URL https://github.com/duckdb/ducklake
    GIT_TAG 08089b16dc8a18ce04abb6efd87e25cb4d64803a
    APPLY_PATCHES
)
duckdb_extension_load(postgres_scanner
    GIT_URL https://github.com/duckdb/duckdb-postgres
    GIT_TAG 8b9d0bbe8de4c57fb90d9e2530f9ce39f8c4baf3
    SUBMODULES database-connector
)

# DuckLake's SQLite catalog, and SQLite files (issue #62: the reporter's DuckLake catalog is SQLite).
duckdb_extension_load(sqlite_scanner
    GIT_URL https://github.com/duckdb/duckdb-sqlite
    GIT_TAG ce0987fda4f6393231c6783ee32f72ab873a6d3e
    SUBMODULES database-connector
)

duckdb_extension_statically_link(core_functions parquet json icu httpfs ducklake postgres_scanner sqlite_scanner)

function(hardcharger_link_libpq)
  find_package(PostgreSQL REQUIRED)
  find_package(OpenSSL REQUIRED)
  target_link_libraries(postgres_scanner_extension PostgreSQL::PostgreSQL OpenSSL::SSL OpenSSL::Crypto)
endfunction()
cmake_language(DEFER DIRECTORY "${CMAKE_SOURCE_DIR}" CALL hardcharger_link_libpq)
