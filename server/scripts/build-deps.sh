#!/usr/bin/env bash
# Builds Hardcharger's pinned dependencies (DuckDB 2.0 and Arrow C++, IPC only) into
# server/.deps, with the warp-sql scripts, and replxx (the client's line editor). DuckDB gets one patch first: the binder hook
# access control needs to check views with their owner's rights (patch-duckdb.cmake).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WARP="$HERE/../third_party/warp-sql/cpp/scripts"
export WARPSQL_DEPS_DIR="$HERE/.deps"
"$WARP/build-arrow.sh"
HARDCHARGER_DEPS_DIR="$HERE/.deps" "$HERE/scripts/build-replxx.sh"

# Check out the commit build-duckdb.sh pins and patch it; build-duckdb.sh then checks
# out the same commit again, which keeps the patched file, and builds.
COMMIT=$(sed -n 's/^DUCKDB_COMMIT=\([0-9a-f]*\)$/\1/p' "$WARP/build-duckdb.sh")
[ -n "$COMMIT" ] || { echo "no DUCKDB_COMMIT in $WARP/build-duckdb.sh" >&2; exit 1; }
SRC="$WARPSQL_DEPS_DIR/duckdb"
mkdir -p "$WARPSQL_DEPS_DIR"
if [ ! -d "$SRC/.git" ]; then
  git clone --quiet --filter=blob:none https://github.com/duckdb/duckdb.git "$SRC"
fi
git -C "$SRC" fetch --quiet --tags origin
git -C "$SRC" checkout --quiet "$COMMIT"
cmake -DDUCKDB_SOURCE_DIR="$SRC" -P "$HERE/scripts/patch-duckdb.cmake"
"$WARP/build-duckdb.sh"
# DuckDB's internal headers, which the access-control module compiles against, include
# a few of DuckDB's vendored libraries' headers that its install leaves out.
THIRD_PARTY="$WARPSQL_DEPS_DIR/duckdb-install/include/duckdb_third_party"
rm -rf "$THIRD_PARTY" && mkdir -p "$THIRD_PARTY"
cp -R "$SRC/third_party/fmt/include/fmt" "$SRC/third_party/re2/re2" \
  "$SRC/third_party/fast_float/fast_float" "$THIRD_PARTY/"
grep -q hardcharger_bound_view_hook "$SRC/src/planner/binder/tableref/bind_basetableref.cpp" \
  || { echo "DuckDB lost the view hook patch" >&2; exit 1; }
