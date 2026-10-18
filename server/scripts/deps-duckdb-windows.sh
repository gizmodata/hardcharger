#!/usr/bin/env bash
# Windows (MSVC): deps-duckdb.sh's recipe for DuckDB with its built-in extensions
# (scripts/duckdb-extensions.cmake), except where Windows differs:
#   - the extensions' libraries (OpenSSL, libpq, libcurl, CRoaring) come from vcpkg's
#     static triplet, through .github/cmake/windows-vcpkg.cmake as CMAKE_TOOLCHAIN_FILE,
#     instead of build-extension-deps.sh (autotools, Linux and macOS);
#   - DuckDB's loadable copies of the extensions are built against a static DuckDB, as
#     DuckDB always does on Windows (its DLL exports the C API only), so the extension
#     config's EXTENSION_STATIC_BUILD FALSE is dropped from a copy of it;
#   - the static link (warp-sql's CMakeLists, WIN32) registers the extensions itself, so
#     no generated loader is copied.
# CI's cache key for it covers deps-duckdb.sh too: keep the two in step.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WARP="$HERE/../third_party/warp-sql/cpp/scripts"
export WARPSQL_DEPS_DIR="${HARDCHARGER_DEPS_DIR:-$HERE/.deps}"
[ -n "${CMAKE_TOOLCHAIN_FILE:-}" ] || { echo "set CMAKE_TOOLCHAIN_FILE (.github/cmake/windows-vcpkg.cmake)" >&2; exit 1; }

COMMIT=$(sed -n 's/^DUCKDB_COMMIT=\([0-9a-f]*\)$/\1/p' "$WARP/build-duckdb.sh")
[ -n "$COMMIT" ] || { echo "no DUCKDB_COMMIT in $WARP/build-duckdb.sh" >&2; exit 1; }
SRC="$WARPSQL_DEPS_DIR/duckdb"
mkdir -p "$WARPSQL_DEPS_DIR"
if [ ! -d "$SRC/.git" ]; then
  git clone --quiet --filter=blob:none https://github.com/duckdb/duckdb.git "$SRC"
fi
git -C "$SRC" fetch --quiet --tags origin
git -C "$SRC" checkout --quiet "$COMMIT"
patches=("$HERE"/scripts/patch-duckdb*.cmake)
[ -f "${patches[0]}" ] || { echo "no scripts/patch-duckdb*.cmake" >&2; exit 1; }
for patch in "${patches[@]}"; do
  cmake -DDUCKDB_SOURCE_DIR="$SRC" -P "$patch"
done

EXTENSIONS="$HERE/scripts/duckdb-extensions.cmake"
for name in httpfs ducklake postgres_scanner; do
  ours=$(awk -v n="$name" '$0 ~ "duckdb_extension_load\\(" n "$" {f = 1} f && /GIT_TAG/ {print $2; exit}' "$EXTENSIONS")
  theirs=$(awk '/GIT_TAG/ {print $2; exit}' "$SRC/.github/config/extensions/$name.cmake")
  [ -n "$ours" ] && [ "$ours" = "$theirs" ] \
    || { echo "$name: duckdb-extensions.cmake pins '$ours', DuckDB $COMMIT pins '$theirs'" >&2; exit 1; }
done
WINDOWS_EXTENSIONS="$WARPSQL_DEPS_DIR/duckdb-extensions-windows.cmake"
grep -v '^set(EXTENSION_STATIC_BUILD FALSE)$' "$EXTENSIONS" > "$WINDOWS_EXTENSIONS"
WARPSQL_DUCKDB_EXTENSION_CONFIGS="$WINDOWS_EXTENSIONS" "$WARP/build-duckdb.sh"

THIRD_PARTY="$WARPSQL_DEPS_DIR/duckdb-install/include/duckdb_third_party"
rm -rf "$THIRD_PARTY" && mkdir -p "$THIRD_PARTY"
cp -R "$SRC/third_party/fmt/include/fmt" "$SRC/third_party/re2/re2" \
  "$SRC/third_party/fast_float/fast_float" "$THIRD_PARTY/"
grep -q hardcharger_bound_view_hook "$SRC/src/planner/binder/tableref/bind_basetableref.cpp" \
  || { echo "DuckDB lost the view hook patch" >&2; exit 1; }
