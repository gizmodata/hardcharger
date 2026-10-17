#!/usr/bin/env bash
# Builds Hardcharger's pinned dependencies (DuckDB 2.0 and Arrow C++, IPC only) into
# server/.deps, with the warp-sql scripts.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WARP="$HERE/../third_party/warp-sql/cpp/scripts"
export WARPSQL_DEPS_DIR="$HERE/.deps"
"$WARP/build-arrow.sh"
"$WARP/build-duckdb.sh"
