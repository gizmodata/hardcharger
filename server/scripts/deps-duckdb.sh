#!/usr/bin/env bash
# Builds DuckDB 2.0 into server/.deps/duckdb-install with warp-sql's build-duckdb.sh, after
# patching it with every scripts/patch-duckdb*.cmake in name order (patch-duckdb.cmake: the
# binder hook access control needs to check views with their owner's rights), and with the
# extensions in scripts/duckdb-extensions.cmake built in (their libraries:
# build-extension-deps.sh). OpenSSL comes from $OPENSSL_ROOT_DIR, else server/.deps's own
# (the portable build's), else the system's. `build-deps.sh duckdb` runs it.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WARP="$HERE/../third_party/warp-sql/cpp/scripts"
export WARPSQL_DEPS_DIR="${HARDCHARGER_DEPS_DIR:-$HERE/.deps}"
if [ -z "${OPENSSL_ROOT_DIR:-}" ] && [ -d "$WARPSQL_DEPS_DIR/openssl-install" ]; then
  export OPENSSL_ROOT_DIR="$WARPSQL_DEPS_DIR/openssl-install"
fi

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
patches=("$HERE"/scripts/patch-duckdb*.cmake)
[ -f "${patches[0]}" ] || { echo "no scripts/patch-duckdb*.cmake" >&2; exit 1; }
for patch in "${patches[@]}"; do
  cmake -DDUCKDB_SOURCE_DIR="$SRC" -P "$patch"
done

# The out-of-tree extensions are pinned where DuckDB's own build of this commit pins them.
EXTENSIONS="$HERE/scripts/duckdb-extensions.cmake"
for name in httpfs ducklake postgres_scanner; do
  ours=$(awk -v n="$name" '$0 ~ "duckdb_extension_load\\(" n "$" {f = 1} f && /GIT_TAG/ {print $2; exit}' "$EXTENSIONS")
  theirs=$(awk '/GIT_TAG/ {print $2; exit}' "$SRC/.github/config/extensions/$name.cmake")
  [ -n "$ours" ] && [ "$ours" = "$theirs" ] \
    || { echo "$name: duckdb-extensions.cmake pins '$ours', DuckDB $COMMIT pins '$theirs'" >&2; exit 1; }
done
"$HERE/scripts/build-extension-deps.sh"
# (The environment variable is ':'-separated, like PATH.)
export CMAKE_PREFIX_PATH="$WARPSQL_DEPS_DIR/roaring-install:$WARPSQL_DEPS_DIR/libpq-install:$WARPSQL_DEPS_DIR/curl-install${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"
if [ -z "${HARDCHARGER_IOS_SDK:-}" ]; then
  WARPSQL_DUCKDB_EXTENSION_CONFIGS="$EXTENSIONS" "$WARP/build-duckdb.sh"
else
  # The iOS app (ios/scripts/build-server-ios.sh): build-duckdb.sh's build of the same
  # commit, cross-compiled with CMAKE_TOOLCHAIN_FILE (ios/cmake/ios.toolchain.cmake), and
  # without loading extensions at run time (ENABLE_EXTENSION_LOAD=OFF): iOS runs only the
  # code the app was signed with, so INSTALL and LOAD have nothing to load, and the
  # extensions above are built in. The platform is named (DuckDB would run a program it
  # builds to find out, which can't run here). Keep the rest in step with build-duckdb.sh.
  [ -n "${CMAKE_TOOLCHAIN_FILE:-}" ] || { echo "HARDCHARGER_IOS_SDK needs CMAKE_TOOLCHAIN_FILE" >&2; exit 2; }
  VERSION_NAME=$(sed -n 's/^DUCKDB_VERSION_NAME=\(v[^ ]*\)$/\1/p' "$WARP/build-duckdb.sh")
  [ -n "$VERSION_NAME" ] || { echo "no DUCKDB_VERSION_NAME in $WARP/build-duckdb.sh" >&2; exit 1; }
  cmake -S "$SRC" -B "$WARPSQL_DEPS_DIR/duckdb-build" -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$CMAKE_TOOLCHAIN_FILE" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$WARPSQL_DEPS_DIR/duckdb-install" \
    -DOVERRIDE_GIT_DESCRIBE="$VERSION_NAME" \
    -DBUILD_SHELL=OFF -DBUILD_UNITTESTS=OFF -DENABLE_SANITIZER=OFF -DENABLE_UBSAN=OFF \
    -DENABLE_EXTENSION_LOAD=OFF \
    -DDUCKDB_EXPLICIT_PLATFORM="$([ "$HARDCHARGER_IOS_SDK" = iphoneos ] && echo ios_arm64 || echo ios_simulator_arm64)" \
    -DBUILD_EXTENSIONS="" -DDUCKDB_EXTENSION_CONFIGS="$EXTENSIONS"
  cmake --build "$WARPSQL_DEPS_DIR/duckdb-build"
  cmake --install "$WARPSQL_DEPS_DIR/duckdb-build"
  cp "$SRC/src/include/duckdb_v2.h" "$WARPSQL_DEPS_DIR/duckdb-install/include/"
fi
# CMake installs libraries into lib64 on RPM-based systems; the server's CMake uses lib.
if [ -d "$WARPSQL_DEPS_DIR/duckdb-install/lib64" ] && [ ! -e "$WARPSQL_DEPS_DIR/duckdb-install/lib" ]; then
  ln -s lib64 "$WARPSQL_DEPS_DIR/duckdb-install/lib"
fi
# DuckDB's internal headers, which the access-control module compiles against, include
# a few of DuckDB's vendored libraries' headers that its install leaves out.
THIRD_PARTY="$WARPSQL_DEPS_DIR/duckdb-install/include/duckdb_third_party"
rm -rf "$THIRD_PARTY" && mkdir -p "$THIRD_PARTY"
cp -R "$SRC/third_party/fmt/include/fmt" "$SRC/third_party/re2/re2" \
  "$SRC/third_party/fast_float/fast_float" "$THIRD_PARTY/"
# For a static link (the release builds, server/CMakeLists.txt HARDCHARGER_STATIC):
# DuckDB's generated loader, which registers every built-in extension through its
# describe function, the file that calls it before main(), and the one archive the
# install leaves out.
BUILD="$WARPSQL_DEPS_DIR/duckdb-build"
LOADER="$WARPSQL_DEPS_DIR/duckdb-install/share/duckdb-static"
mkdir -p "$LOADER"
cp "$BUILD/src/duckdb_static_extension_loader.c" "$SRC/extension/loader/static_extension_autoregister.cpp" "$LOADER/"
cp "$BUILD/src/main/extension/local_extension_repository/libduckdb_local_extension_repository.a" \
  "$WARPSQL_DEPS_DIR/duckdb-install/lib/"
# The static libraries the extensions link, kept with the install (it is what CI caches).
STATIC_DEPS="$WARPSQL_DEPS_DIR/duckdb-install/lib/static-deps"
rm -rf "$STATIC_DEPS" && mkdir -p "$STATIC_DEPS"
cp "$WARPSQL_DEPS_DIR/libpq-install/lib/libpq.a" "$WARPSQL_DEPS_DIR/roaring-install/lib/libroaring.a" "$STATIC_DEPS/"
if [ -f "$WARPSQL_DEPS_DIR/curl-install/lib/libcurl.a" ]; then cp "$WARPSQL_DEPS_DIR/curl-install/lib/libcurl.a" "$STATIC_DEPS/"; fi
grep -q hardcharger_bound_view_hook "$SRC/src/planner/binder/tableref/bind_basetableref.cpp" \
  || { echo "DuckDB lost the view hook patch" >&2; exit 1; }
grep -q HardchargerDecimal38Converter "$SRC/src/common/arrow/arrow_appender.cpp" \
  || { echo "DuckDB lost the HUGEINT / UHUGEINT Arrow export patch" >&2; exit 1; }
