#!/usr/bin/env bash
# Test-only DuckDB fixture: optional extensions must share TSan's instrumentation.
# Published binaries keep the normal build-deps.sh recipe and runtime extensions.
set -euo pipefail
[[ "${CXXFLAGS:-}" == *-fsanitize=thread* ]] \
  || { echo "build-tsan-duckdb.sh requires -fsanitize=thread in CXXFLAGS" >&2; exit 2; }
[ "${1:-}" = duckdb ] && [ "$#" -eq 1 ] \
  || { echo "usage: build-tsan-duckdb.sh duckdb" >&2; exit 2; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export WARPSQL_DEPS_DIR="${HARDCHARGER_DEPS_DIR:-$HERE/.deps}"
# First use the standard recipe for checkout, patches, installed internal headers and
# loader files. Reconfiguring that same build reuses the instrumented engine objects.
"$HERE/scripts/build-deps.sh" duckdb
"$HERE/scripts/build-extension-deps.sh"
export CMAKE_PREFIX_PATH="$WARPSQL_DEPS_DIR/roaring-install:$WARPSQL_DEPS_DIR/libpq-install:$WARPSQL_DEPS_DIR/curl-install${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"
BUILD="$WARPSQL_DEPS_DIR/duckdb-build"
cmake -S "$WARPSQL_DEPS_DIR/duckdb" -B "$BUILD" \
  -DDUCKDB_EXTENSION_CONFIGS="$HERE/scripts/duckdb-extensions-tsan.cmake"
cmake --build "$BUILD"
cmake --install "$BUILD"
cp "$BUILD/src/duckdb_static_extension_loader.c" \
  "$WARPSQL_DEPS_DIR/duckdb-install/share/duckdb-static/"
