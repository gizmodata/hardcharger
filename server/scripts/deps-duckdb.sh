#!/usr/bin/env bash
# Build the pinned Hardcharger Engine source for local development.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export CHARGEWIRE_DEPS_DIR="${HARDCHARGER_DEPS_DIR:-$HERE/.deps}"
if [ -z "${OPENSSL_ROOT_DIR:-}" ] && [ -d "$CHARGEWIRE_DEPS_DIR/openssl-install" ]; then
  export OPENSSL_ROOT_DIR="$CHARGEWIRE_DEPS_DIR/openssl-install"
fi

source "$HERE/scripts/duckdb-source.sh"
# Earlier releases applied patches directly to the cached DuckDB checkout. Keep
# that worktree and its CMake build directory intact when switching to the fork.
SRC="$CHARGEWIRE_DEPS_DIR/hardcharger-engine/duckdb"
BUILD="$CHARGEWIRE_DEPS_DIR/hardcharger-engine/duckdb-build"
prepare_duckdb_source

# Mobile apps build the optional extensions and their transitive libraries. Desktop builds
# retain DuckDB's base four; INSTALL/LOAD uses signed release-compatible modules.
EXTENSIONS="$HERE/scripts/duckdb-extensions.cmake"
# The baseline Linux x86_64 build (scripts/cpu-target.sh) builds in the extensions people load
# most, as the mobile builds do: DuckDB's downloadable ones need x86-64-v3 (issue #62).
BASELINE_LINUX=false
if [ -z "${HARDCHARGER_IOS_SDK:-}${HARDCHARGER_ANDROID_ABI:-}" ] && [ "$(uname)" = Linux ] &&
   [ "$(uname -m)" = x86_64 ] && [ "$("$HERE/scripts/cpu-target.sh")" = baseline ]; then
  BASELINE_LINUX=true
fi
if [ -n "${HARDCHARGER_IOS_SDK:-}${HARDCHARGER_ANDROID_ABI:-}" ] || $BASELINE_LINUX; then
  EXTENSIONS="$HERE/scripts/duckdb-extensions-ios.cmake"
  $BASELINE_LINUX && EXTENSIONS="$HERE/scripts/duckdb-extensions-baseline.cmake"
  pinned="httpfs ducklake postgres_scanner"
  $BASELINE_LINUX && pinned="$pinned sqlite_scanner"
  for name in $pinned; do
    ours=$(awk -v n="$name" '$0 ~ "duckdb_extension_load\\(" n "$" {f = 1} f && /GIT_TAG/ {print $2; exit}' "$EXTENSIONS")
    theirs=$(awk '/GIT_TAG/ {print $2; exit}' "$SRC/.github/config/extensions/$name.cmake")
    [ -n "$ours" ] && [ "$ours" = "$theirs" ] \
      || { echo "$name: mobile pins '$ours', DuckDB $DUCKDB_COMMIT pins '$theirs'" >&2; exit 1; }
  done
  if [ -n "${HARDCHARGER_ANDROID_ABI:-}" ]; then EXTENSIONS="$HERE/scripts/duckdb-extensions-android.cmake"; fi
  "$HERE/scripts/build-extension-deps.sh"
  export CMAKE_PREFIX_PATH="$CHARGEWIRE_DEPS_DIR/roaring-install:$CHARGEWIRE_DEPS_DIR/libpq-install:$CHARGEWIRE_DEPS_DIR/curl-install${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"
fi
if [ -n "${HARDCHARGER_ANDROID_ABI:-}" ]; then
  platform=android_arm64
  [ "$HARDCHARGER_ANDROID_ABI" != x86_64 ] || platform=android_amd64
  cmake -S "$SRC" -B "$BUILD" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$CHARGEWIRE_DEPS_DIR/duckdb-install" \
    -DOVERRIDE_GIT_DESCRIBE="$DUCKDB_VERSION_NAME" -DDUCKDB_EXPLICIT_PLATFORM="$platform" \
    -DBUILD_SHELL=OFF -DBUILD_UNITTESTS=OFF -DENABLE_SANITIZER=OFF -DENABLE_UBSAN=OFF \
    -DBUILD_EXTENSIONS="" -DDUCKDB_EXTENSION_CONFIGS="$EXTENSIONS"
  # Loadable module targets deliberately reference symbols in the host engine. They
  # are not app artifacts; build the two engine libraries and their static dependencies.
  cmake --build "$BUILD" --target duckdb duckdb_static duckdb_cpp_api duckdb_local_extension_repository
  cmake --install "$BUILD"
  cp "$SRC/src/include/duckdb_v2.h" "$CHARGEWIRE_DEPS_DIR/duckdb-install/include/"
elif [ -z "${HARDCHARGER_IOS_SDK:-}" ]; then
  # HARDCHARGER_CPU_TARGET (scripts/cpu-target.sh): optimized keeps DuckDB's default profile
  # (CLI: x86-64-v3 on Linux x86_64), baseline builds for any x86-64 CPU (NONE). Always
  # passed explicitly, so a reused build directory never keeps the other target's setting.
  PROFILE=CLI
  [ "$("$HERE/scripts/cpu-target.sh")" != baseline ] || PROFILE=NONE
  build_native_duckdb "$EXTENSIONS" "$PROFILE"
else
  # The iOS app (ios/scripts/build-server-ios.sh): build of the same engine
  # commit, cross-compiled with CMAKE_TOOLCHAIN_FILE (ios/cmake/ios.toolchain.cmake), and
  # without loading extensions at run time (ENABLE_EXTENSION_LOAD=OFF): iOS runs only the
  # code the app was signed with, so INSTALL and LOAD have nothing to load, and the
  # extensions above are built in. The platform is named (DuckDB would run a program it
  # builds to find out, which can't run here). Keep the extension ABI version in step with native builds.
  [ -n "${CMAKE_TOOLCHAIN_FILE:-}" ] || { echo "HARDCHARGER_IOS_SDK needs CMAKE_TOOLCHAIN_FILE" >&2; exit 2; }
  cmake -S "$SRC" -B "$BUILD" -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$CMAKE_TOOLCHAIN_FILE" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$CHARGEWIRE_DEPS_DIR/duckdb-install" \
    -DOVERRIDE_GIT_DESCRIBE="$DUCKDB_VERSION_NAME" \
    -DBUILD_SHELL=OFF -DBUILD_UNITTESTS=OFF -DENABLE_SANITIZER=OFF -DENABLE_UBSAN=OFF \
    -DENABLE_EXTENSION_LOAD=OFF \
    -DDUCKDB_EXPLICIT_PLATFORM="$([ "$HARDCHARGER_IOS_SDK" = iphoneos ] && echo ios_arm64 || echo ios_simulator_arm64)" \
    -DBUILD_EXTENSIONS="" -DDUCKDB_EXTENSION_CONFIGS="$EXTENSIONS"
  cmake --build "$BUILD"
  cmake --install "$BUILD"
  cp "$SRC/src/include/duckdb_v2.h" "$CHARGEWIRE_DEPS_DIR/duckdb-install/include/"
fi
# CMake installs libraries into lib64 on RPM-based systems; the server's CMake uses lib.
if [ -d "$CHARGEWIRE_DEPS_DIR/duckdb-install/lib64" ] && [ ! -e "$CHARGEWIRE_DEPS_DIR/duckdb-install/lib" ]; then
  ln -s lib64 "$CHARGEWIRE_DEPS_DIR/duckdb-install/lib"
fi
# DuckDB's internal headers, which the access-control module compiles against, include
# a few of DuckDB's vendored libraries' headers that its install leaves out.
THIRD_PARTY="$CHARGEWIRE_DEPS_DIR/duckdb-install/include/duckdb_third_party"
rm -rf "$THIRD_PARTY" && mkdir -p "$THIRD_PARTY"
cp -R "$SRC/third_party/fmt/include/fmt" "$SRC/third_party/re2/re2" \
  "$SRC/third_party/fast_float/fast_float" "$THIRD_PARTY/"
# For a static link (the release builds, server/CMakeLists.txt HARDCHARGER_STATIC):
# DuckDB's generated loader, which registers every built-in extension through its
# describe function, the file that calls it before main(), and the one archive the
# install leaves out.
LOADER="$CHARGEWIRE_DEPS_DIR/duckdb-install/share/duckdb-static"
mkdir -p "$LOADER"
cp "$BUILD/src/duckdb_static_extension_loader.c" "$SRC/extension/loader/static_extension_autoregister.cpp" "$LOADER/"
cp "$BUILD/src/main/extension/local_extension_repository/libduckdb_local_extension_repository.a" \
  "$CHARGEWIRE_DEPS_DIR/duckdb-install/lib/"
# The static libraries the extensions link, kept with the install (it is what CI caches).
STATIC_DEPS="$CHARGEWIRE_DEPS_DIR/duckdb-install/lib/static-deps"
rm -rf "$STATIC_DEPS" && mkdir -p "$STATIC_DEPS"
if [ -n "${HARDCHARGER_IOS_SDK:-}${HARDCHARGER_ANDROID_ABI:-}" ] || $BASELINE_LINUX; then
  cp "$CHARGEWIRE_DEPS_DIR/libpq-install/lib/libpq.a" "$CHARGEWIRE_DEPS_DIR/roaring-install/lib/libroaring.a" "$STATIC_DEPS/"
  cp "$CHARGEWIRE_DEPS_DIR/curl-install/lib/libcurl.a" "$STATIC_DEPS/"
fi
if [ -z "${HARDCHARGER_IOS_SDK:-}${HARDCHARGER_ANDROID_ABI:-}" ] && ! $BASELINE_LINUX; then
  cmake -DDUCKDB_INSTALL_DIR="$CHARGEWIRE_DEPS_DIR/duckdb-install" -P "$HERE/scripts/prune-duckdb-install.cmake"
fi
# What the server's CMake reads to know which CPUs this DuckDB runs on (cpu-target.sh).
mkdir -p "$CHARGEWIRE_DEPS_DIR/duckdb-install/share"
"$HERE/scripts/cpu-target.sh" > "$CHARGEWIRE_DEPS_DIR/duckdb-install/share/hardcharger-cpu-target"
