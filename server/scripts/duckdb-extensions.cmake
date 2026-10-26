# Native Linux, macOS and Windows builds use DuckDB's four default built-ins only.
# Optional extensions (including autocomplete) use INSTALL/LOAD and can be replaced
# from a compatible repository or extension file. iOS uses duckdb-extensions-ios.cmake.
set(EXTENSION_STATIC_BUILD FALSE)
# Exclude DuckDB's development-only local repository capability. Autoinstall must use
# the configured/core repository, never an unshipped dependency-build directory.
set(DUCKDB_CAPABILITIES "httplib;loadable_extensions" CACHE STRING "" FORCE)
duckdb_extension_load(core_functions)
duckdb_extension_load(parquet)
duckdb_extension_load(json)
duckdb_extension_load(icu)
duckdb_extension_statically_link(core_functions parquet json icu)
