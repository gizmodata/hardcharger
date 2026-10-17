#!/usr/bin/env bash
# Builds replxx, the command-line client's line editor (history, completion, highlighting),
# at its latest release into server/.deps/replxx-install. build-deps.sh runs it; CI caches
# the result with DuckDB and Arrow, keyed on this file.
set -euo pipefail

REPLXX_TAG=release-0.0.4

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPS="${HARDCHARGER_DEPS_DIR:-$HERE/.deps}"
SRC="$DEPS/replxx"
BUILD="$DEPS/replxx-build"
PREFIX="$DEPS/replxx-install"

mkdir -p "$DEPS"
if [ ! -d "$SRC/.git" ]; then
  git clone --quiet --depth 1 --branch "$REPLXX_TAG" https://github.com/AmokHuginnsson/replxx.git "$SRC"
fi
# A checkout of another tag (an older pin) is rebuilt from this one.
have=$(git -C "$SRC" describe --tags --exact-match 2>/dev/null || true)
if [ "$have" != "$REPLXX_TAG" ]; then
  git -C "$SRC" fetch --quiet --depth 1 origin tag "$REPLXX_TAG"
  git -C "$SRC" checkout --quiet "$REPLXX_TAG"
  rm -rf "$BUILD" "$PREFIX"
fi

generator=()
if command -v ninja >/dev/null 2>&1; then generator=(-G Ninja); fi
# MSVC: the static C runtime (/MT), as DuckDB and everything linked with it.
platform_args=()
case "$(uname -s)" in
  MINGW* | MSYS* | CYGWIN*)
    platform_args+=(-DCMAKE_POLICY_DEFAULT_CMP0091=NEW "-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded") ;;
esac
cmake -S "$SRC" -B "$BUILD" "${generator[@]}" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DCMAKE_INSTALL_LIBDIR=lib \
  -DCMAKE_INSTALL_INCLUDEDIR=include \
  -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
  -DBUILD_SHARED_LIBS=OFF \
  -DREPLXX_BUILD_EXAMPLES=OFF \
  -DREPLXX_BUILD_PACKAGE=OFF \
  ${platform_args[@]+"${platform_args[@]}"}
cmake --build "$BUILD" --config Release
cmake --install "$BUILD" --config Release

echo "replxx $REPLXX_TAG installed in $PREFIX"
