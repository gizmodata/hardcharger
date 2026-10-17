#!/usr/bin/env bash
# Prints the CI cache key for server/.deps (the DuckDB, Arrow and replxx installs build-deps.sh
# makes): the platform, the exact toolchain that compiled them, and a hash of every
# recipe they come from. A runner image that moves to a new compiler, or any change to
# a recipe, gets a new key and a cold build instead of a cache whose objects or pins no
# longer match.
#
#   server/scripts/ci-deps-key.sh <platform> <toolchain> [more recipe files...]
#
# <toolchain> is a description of the compiler (e.g. `c++ --version | head -1`, or the
# Xcode and clang builds); it goes into the key as readable text. There is no restore
# prefix to share: the cache holds finished installs, never a build tree a later run
# could resume, so a key that differs in anything must start cold.
set -euo pipefail
platform="${1:?platform}"
toolchain="${2:?toolchain}"
shift 2
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
recipes=(
  third_party/warp-sql/cpp/scripts/build-arrow.sh
  third_party/warp-sql/cpp/scripts/build-duckdb.sh
  server/scripts/build-deps.sh
  server/scripts/build-replxx.sh
  server/scripts/patch-duckdb.cmake
  server/scripts/ci-deps-key.sh
  "$@"
)
for recipe in "${recipes[@]}"; do
  [ -f "$recipe" ] || { echo "ci-deps-key: no recipe $recipe" >&2; exit 1; }
done
# Readable and cache-key safe: runs of anything else become one '-'.
tool=$(printf '%s' "$toolchain" | tr -cs 'A-Za-z0-9.' '-' | sed 's/^-//; s/-$//')
[ -n "$tool" ] || { echo "ci-deps-key: empty toolchain" >&2; exit 1; }
sum=$(cat "${recipes[@]}" | shasum -a 256 | cut -c1-16)
echo "deps-v2-$platform-$tool-$sum"
