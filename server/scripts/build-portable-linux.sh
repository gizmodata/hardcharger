#!/usr/bin/env bash
# Builds the Linux server with a glibc 2.28 baseline, so the release tarball runs on any
# distribution with glibc 2.28 or newer (Oracle/RHEL/Alma/Rocky Linux 8, Debian 10+,
# Ubuntu 20.04+, Amazon Linux 2023, ...), not only on the runner image it was built on.
#
# Runs INSIDE quay.io/pypa/manylinux_2_28_{x86_64,aarch64} (AlmaLinux 8, glibc 2.28,
# gcc-toolset-14 first on PATH; the toolset links the newer-than-EL8 parts of libstdc++
# statically, so the binaries' GLIBCXX needs stay at EL8's). Mount the checkout at the
# same path as on the host, so the absolute paths CMake records (the build RPATH to
# server/.deps/duckdb-install/lib) still hold when the tests run the binaries on the host:
#
#   docker run --rm -v "$PWD:$PWD" -w "$PWD" quay.io/pypa/manylinux_2_28_$(uname -m) \
#     server/scripts/build-portable-linux.sh deps
#   docker run ... server/scripts/build-portable-linux.sh server <build dir> [cmake args...]
#
# server/scripts/in-manylinux.sh runs it so in CI. `deps` builds DuckDB, Arrow and a static OpenSSL 3 (EL8 ships 1.1.1; static, so the
# binaries need no libssl at all) into server/.deps, which CI caches. `server` builds the
# server against them (every compile through sccache when it is on PATH), then fails when
# a shipped file (the server, libduckdb) needs a glibc symbol newer than 2.28 or a
# GLIBCXX newer than EL8's 3.4.25.
set -euo pipefail

MODE="${1:?deps | server <build dir> [cmake args...] | build <build dir> [cmake --build args...]}"
shift
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEPS="$ROOT/server/.deps"
OPENSSL_VERSION=3.5.9
OPENSSL_SHA256=603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a
OPENSSL_PREFIX="$DEPS/openssl-install"

cd "$ROOT"
# The checkout (and DuckDB's clone in server/.deps) belongs to the host's runner user and
# this container runs as root: without this, git refuses the repository ("dubious
# ownership") and DuckDB's own version detection quietly goes wrong.
git config --global --add safe.directory '*'
# Everything this writes goes back to the host user who ran the container (in-manylinux.sh
# passes HOST_UID/HOST_GID), so the host's later steps can use and cache it.
give_back() {
  if [ -n "${HOST_UID:-}" ]; then
    chown -R "$HOST_UID:${HOST_GID:-$HOST_UID}" "$DEPS" ${BUILD_DIR:+"$BUILD_DIR"} 2>/dev/null || true
  fi
}
trap give_back EXIT

# Build tools only: nothing here becomes a runtime dependency.
dnf install -y -q --setopt=install_weak_deps=False \
  ninja-build perl-IPC-Cmd perl-Pod-Html perl-Time-Piece >/dev/null

build_deps() {
  if [ ! -f "$OPENSSL_PREFIX/.openssl-$OPENSSL_VERSION.stamp" ]; then
    local src
    src=$(mktemp -d)
    curl -fsSL -o "$src/openssl.tar.gz" \
      "https://github.com/openssl/openssl/releases/download/openssl-$OPENSSL_VERSION/openssl-$OPENSSL_VERSION.tar.gz"
    echo "$OPENSSL_SHA256  $src/openssl.tar.gz" | sha256sum -c -
    tar -xzf "$src/openssl.tar.gz" -C "$src" --strip-components=1
    (cd "$src" && ./config no-shared no-tests no-docs --prefix="$OPENSSL_PREFIX" --libdir=lib \
       && make -j"${CMAKE_BUILD_PARALLEL_LEVEL:-$(nproc)}" build_libs >/dev/null && make install_dev >/dev/null)
    rm -rf "$src"
    touch "$OPENSSL_PREFIX/.openssl-$OPENSSL_VERSION.stamp"
  fi
  # Arrow's bundled uriparser calls reallocarray when CMake finds it. glibc 2.28 has it
  # but declares it only under _GNU_SOURCE (2.29 moved it to _DEFAULT_SOURCE, which is
  # all uriparser asks for), and gcc 14 refuses the implicit declaration. Arrow's C code
  # (uriparser, lz4, zstd) is built with _GNU_SOURCE, as g++ builds all C++ anyway;
  # build-deps.sh then finds Arrow built and goes on to DuckDB, whose C is untouched.
  CFLAGS="${CFLAGS:-} -D_GNU_SOURCE" WARPSQL_DEPS_DIR="$DEPS" third_party/warp-sql/cpp/scripts/build-arrow.sh
  server/scripts/build-deps.sh
  # CMake installs libraries into lib64 on RPM-based systems; the server's CMake, its
  # packaging and the Debian/macOS builds all use lib.
  local prefix
  for prefix in "$DEPS/duckdb-install" "$DEPS/arrow-install"; do
    if [ -d "$prefix/lib64" ] && [ ! -e "$prefix/lib" ]; then ln -s lib64 "$prefix/lib"; fi
  done
  [ -f "$DEPS/duckdb-install/lib/libduckdb.so" ] || { echo "no libduckdb.so in $DEPS/duckdb-install/lib" >&2; exit 1; }
}

build_server() {
  local build_dir="${1:?build dir, e.g. server/build}"
  shift
  BUILD_DIR="$build_dir"
  local launcher=()
  if command -v sccache >/dev/null 2>&1; then
    launcher=(-DCMAKE_C_COMPILER_LAUNCHER=sccache -DCMAKE_CXX_COMPILER_LAUNCHER=sccache)
  fi
  cmake -S server -B "$build_dir" -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DOPENSSL_ROOT_DIR="$OPENSSL_PREFIX" -DOPENSSL_USE_STATIC_LIBS=TRUE \
    "${launcher[@]}" "$@"
  cmake --build "$build_dir"
  if command -v sccache >/dev/null 2>&1; then sccache --show-stats || true; fi
  server/scripts/check-glibc-baseline.sh "$build_dir/hardcharger-server" "$build_dir/hardcharger" \
    "$DEPS"/duckdb-install/lib/libduckdb.so
}

case "$MODE" in
  deps) build_deps ;;
  server) build_server "$@" ;;
  # More targets of a configured build dir: build <build dir> [cmake --build args...]
  build) BUILD_DIR="${1:?build dir}"; cmake --build "$@" ;;
  *) echo "unknown mode $MODE (deps | server <build dir> [cmake args...] | build <build dir> [args...])" >&2; exit 2 ;;
esac
