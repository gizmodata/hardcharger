#!/usr/bin/env bash
# Builds Hardcharger's pinned dependencies into server/.deps, each by its own recipe, so
# each can be built (and cached) alone:
#
#   arrow   Arrow C++, IPC only            deps-arrow.sh (chargewire's build-arrow.sh)
#   duckdb  DuckDB 2.0, patched, with its  deps-duckdb.sh (chargewire's build-duckdb.sh,
#           built-in extensions              patch-duckdb*.cmake, duckdb-extensions.cmake,
#                                            build-extension-deps.sh)
#   replxx  the client's line editor       build-replxx.sh
#   openssl a static OpenSSL 3, for the    deps-openssl.sh
#           portable Linux build only
#
#   server/scripts/build-deps.sh                  # arrow, replxx and duckdb
#   server/scripts/build-deps.sh duckdb replxx    # just those, in that order
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export HARDCHARGER_DEPS_DIR="${HARDCHARGER_DEPS_DIR:-$HERE/.deps}"
[ "$#" -gt 0 ] || set -- arrow replxx duckdb
for dep in "$@"; do
  case "$dep" in
    arrow) "$HERE/scripts/deps-arrow.sh" ;;
    duckdb) "$HERE/scripts/deps-duckdb.sh" ;;
    replxx) "$HERE/scripts/build-replxx.sh" ;;
    openssl) "$HERE/scripts/deps-openssl.sh" ;;
    *) echo "build-deps.sh: no dependency '$dep' (arrow, duckdb, replxx, openssl)" >&2; exit 2 ;;
  esac
done
