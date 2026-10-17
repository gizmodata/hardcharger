#!/usr/bin/env bash
# Prints the CI cache key for one dependency's install in server/.deps (see
# build-deps.sh): the dependency, the platform (with the sanitizer, for instrumented
# builds), the exact toolchain that compiled it, and a hash of that dependency's own
# recipes only. A runner image's new compiler, or a change to a recipe, rebuilds just the
# dependencies it touches: a replxx bump never rebuilds DuckDB.
#
#   server/scripts/ci-deps-key.sh <dependency> <platform> <toolchain> [more recipe files...]
#
# <toolchain> describes the compiler (e.g. `c++ --version | head -1`, the Xcode and clang
# builds, or the manylinux image); it goes into the key as readable text. Each cache holds
# a finished install, never a build tree a later run could resume, so a key that differs
# in anything starts cold and there is no restore prefix. Changing what a key is made of
# means a new generation (v3 below).
set -euo pipefail
dep="${1:?dependency: arrow | duckdb | replxx | openssl}"
platform="${2:?platform}"
toolchain="${3:?toolchain}"
shift 3
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
case "$dep" in
  arrow) recipes=(third_party/warp-sql/cpp/scripts/build-arrow.sh server/scripts/deps-arrow.sh) ;;
  duckdb) recipes=(third_party/warp-sql/cpp/scripts/build-duckdb.sh server/scripts/deps-duckdb.sh
                   server/scripts/patch-duckdb*.cmake server/scripts/duckdb-extensions.cmake
                   server/scripts/build-extension-deps.sh) ;;
  replxx) recipes=(server/scripts/build-replxx.sh) ;;
  openssl) recipes=(server/scripts/deps-openssl.sh) ;;
  *) echo "ci-deps-key: no dependency '$dep'" >&2; exit 2 ;;
esac
recipes+=("$@")
for recipe in "${recipes[@]}"; do
  [ -f "$recipe" ] || { echo "ci-deps-key: no recipe $recipe" >&2; exit 1; }
done
# Readable and cache-key safe: runs of anything else become one '-'.
tool=$(printf '%s' "$toolchain" | tr -cs 'A-Za-z0-9.' '-' | sed 's/^-//; s/-$//')
[ -n "$tool" ] || { echo "ci-deps-key: empty toolchain" >&2; exit 1; }
sum=$(cat "${recipes[@]}" | shasum -a 256 | cut -c1-16)
echo "deps-$dep-v3-$platform-$tool-$sum"
