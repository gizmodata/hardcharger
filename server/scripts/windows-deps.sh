#!/usr/bin/env bash
# Windows (MSVC): builds one of server/.deps' dependencies, as build-deps.sh does
# elsewhere, with what MSVC needs on top (CI's cache key for each covers this file):
#   arrow   build-deps.sh arrow, on the static C runtime (/MT: DuckDB always builds with it)
#   replxx  build-deps.sh replxx, likewise; replxx asks for CMake 3.5, whose policies ignore
#           CMAKE_MSVC_RUNTIME_LIBRARY (CMP0091 is 3.15's), so its policies start at 3.15
#   duckdb  deps-duckdb-windows.sh, with vcpkg's static libraries for its extensions
# windows.yml runs it through .github/actions/dep-cache (in a Developer shell, with
# VCPKG_ROOT and VCPKG_TRIPLET set).
#
#   server/scripts/windows-deps.sh arrow|replxx|duckdb
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CMAKE_DIR="$(cygpath -m "$HERE/../.github/cmake")"
case "${1:?arrow, replxx or duckdb}" in
  arrow)
    CMAKE_TOOLCHAIN_FILE="$CMAKE_DIR/msvc-static-crt.cmake" "$HERE/scripts/build-deps.sh" arrow ;;
  replxx)
    CMAKE_TOOLCHAIN_FILE="$CMAKE_DIR/msvc-static-crt.cmake" CMAKE_POLICY_VERSION_MINIMUM=3.15 \
      "$HERE/scripts/build-deps.sh" replxx ;;
  duckdb)
    CMAKE_TOOLCHAIN_FILE="$CMAKE_DIR/windows-vcpkg.cmake" "$HERE/scripts/deps-duckdb-windows.sh" ;;
  *) echo "windows-deps.sh: no dependency '$1' (arrow, replxx, duckdb)" >&2; exit 2 ;;
esac
