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
CHARGEWIRE="$HERE/../third_party/chargewire/cpp/scripts"
export CHARGEWIRE_DEPS_DIR="${HARDCHARGER_DEPS_DIR:-$HERE/.deps}"
[ -n "${CMAKE_TOOLCHAIN_FILE:-}" ] || { echo "set CMAKE_TOOLCHAIN_FILE (.github/cmake/windows-vcpkg.cmake)" >&2; exit 1; }

COMMIT=$(sed -n 's/^DUCKDB_COMMIT=\([0-9a-f]*\)$/\1/p' "$CHARGEWIRE/build-duckdb.sh")
[ -n "$COMMIT" ] || { echo "no DUCKDB_COMMIT in $CHARGEWIRE/build-duckdb.sh" >&2; exit 1; }
SRC="$CHARGEWIRE_DEPS_DIR/duckdb"
mkdir -p "$CHARGEWIRE_DEPS_DIR"
if [ ! -e "$SRC/.git" ]; then
  git clone --quiet --filter=blob:none https://github.com/duckdb/duckdb.git "$SRC"
fi
git -C "$SRC" fetch --quiet --tags origin
git -C "$SRC" checkout --quiet "$COMMIT"
cmake -DDUCKDB_SOURCE_DIR="$SRC" -P "$HERE/scripts/retire-ducklake-patch.cmake"
patches=("$HERE"/scripts/patch-duckdb*.cmake)
[ -f "${patches[0]}" ] || { echo "no scripts/patch-duckdb*.cmake" >&2; exit 1; }
for patch in "${patches[@]}"; do
  cmake -DDUCKDB_SOURCE_DIR="$SRC" -P "$patch"
done

# Windows keeps DuckDB's standard static-extension link behavior (DLL exports are C API).
WINDOWS_EXTENSIONS="$CHARGEWIRE_DEPS_DIR/duckdb-extensions-windows.cmake"
grep -v '^set(EXTENSION_STATIC_BUILD FALSE)$' "$HERE/scripts/duckdb-extensions.cmake" > "$WINDOWS_EXTENSIONS"
CHARGEWIRE_DUCKDB_EXTENSION_CONFIGS="$WINDOWS_EXTENSIONS" "$CHARGEWIRE/build-duckdb.sh"
cmake -DDUCKDB_INSTALL_DIR="$CHARGEWIRE_DEPS_DIR/duckdb-install" -P "$HERE/scripts/prune-duckdb-install.cmake"

THIRD_PARTY="$CHARGEWIRE_DEPS_DIR/duckdb-install/include/duckdb_third_party"
rm -rf "$THIRD_PARTY" && mkdir -p "$THIRD_PARTY"
cp -R "$SRC/third_party/fmt/include/fmt" "$SRC/third_party/re2/re2" \
  "$SRC/third_party/fast_float/fast_float" "$THIRD_PARTY/"
grep -q hardcharger_bound_view_hook "$SRC/src/planner/binder/tableref/bind_basetableref.cpp" \
  || { echo "DuckDB lost the view hook patch" >&2; exit 1; }
grep -q HardchargerDecimal38Converter "$SRC/src/common/arrow/arrow_appender.cpp" \
  || { echo "DuckDB lost the HUGEINT / UHUGEINT Arrow export patch" >&2; exit 1; }
