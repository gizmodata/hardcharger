#!/usr/bin/env bash
# Windows (MSVC): deps-duckdb.sh's recipe for DuckDB with its built-in extensions
# (scripts/duckdb-extensions.cmake), except where Windows differs:
#   - OpenSSL comes from vcpkg's
#     static triplet, through .github/cmake/windows-vcpkg.cmake as CMAKE_TOOLCHAIN_FILE,
#     optional extension libraries are supplied by their loadable modules;
#   - DuckDB's loadable copies of the extensions are built against a static DuckDB, as
#     DuckDB always does on Windows (its DLL exports the C API only), so the extension
#     config's EXTENSION_STATIC_BUILD FALSE is dropped from a copy of it;
#   - the static link (chargewire's CMakeLists, WIN32) registers the extensions itself, so
#     no generated loader is copied.
# CI's cache key for it covers deps-duckdb.sh too: keep the two in step.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export CHARGEWIRE_DEPS_DIR="${HARDCHARGER_DEPS_DIR:-$HERE/.deps}"
[ -n "${CMAKE_TOOLCHAIN_FILE:-}" ] || { echo "set CMAKE_TOOLCHAIN_FILE (.github/cmake/windows-vcpkg.cmake)" >&2; exit 1; }

source "$HERE/scripts/duckdb-source.sh"
# Earlier releases applied patches directly to the cached DuckDB checkout. Keep
# that worktree and its CMake build directory intact when switching to the fork.
SRC="$CHARGEWIRE_DEPS_DIR/hardcharger-engine/duckdb"
prepare_duckdb_source

# Windows keeps DuckDB's standard static-extension link behavior (DLL exports are C API).
WINDOWS_EXTENSIONS="$CHARGEWIRE_DEPS_DIR/duckdb-extensions-windows.cmake"
grep -v '^set(EXTENSION_STATIC_BUILD FALSE)$' "$HERE/scripts/duckdb-extensions.cmake" > "$WINDOWS_EXTENSIONS"
build_native_duckdb "$WINDOWS_EXTENSIONS" CLI
cmake -DDUCKDB_INSTALL_DIR="$CHARGEWIRE_DEPS_DIR/duckdb-install" -P "$HERE/scripts/prune-duckdb-install.cmake"

THIRD_PARTY="$CHARGEWIRE_DEPS_DIR/duckdb-install/include/duckdb_third_party"
rm -rf "$THIRD_PARTY" && mkdir -p "$THIRD_PARTY"
cp -R "$SRC/third_party/fmt/include/fmt" "$SRC/third_party/re2/re2" \
  "$SRC/third_party/fast_float/fast_float" "$THIRD_PARTY/"
