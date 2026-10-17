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
#     server/scripts/build-portable-linux.sh deps openssl arrow replxx duckdb
#   docker run ... server/scripts/build-portable-linux.sh server <build dir> [cmake args...]
#
# server/scripts/in-manylinux.sh runs it so in CI. `deps` builds the dependencies named
# (build-deps.sh: openssl, a static OpenSSL 3 since EL8 ships 1.1.1, then arrow, replxx,
# duckdb) into server/.deps, which CI caches one by one. `server` builds the
# server against them (every compile through sccache when it is on PATH), then fails when
# a shipped file (the server, libduckdb) needs a glibc symbol newer than 2.28 or a
# GLIBCXX newer than EL8's 3.4.25.
set -euo pipefail

MODE="${1:?deps <dependency...> | server <build dir> [cmake args...] | build <build dir> [cmake --build args...]}"
shift
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEPS="$ROOT/server/.deps"
OPENSSL_PREFIX="$DEPS/openssl-install"  # deps-openssl.sh's

cd "$ROOT"
# The checkout (and DuckDB's clone in server/.deps) belongs to the host's runner user and
# this container runs as root: without this, git refuses the repository ("dubious
# ownership") and DuckDB's own version detection quietly goes wrong.
git config --global --add safe.directory '*'
# Everything this writes goes back to the host user who ran the container (in-manylinux.sh
# passes HOST_UID/HOST_GID), so the host's later steps can use and cache it.
give_back() {
  if [ -n "${HOST_UID:-}" ]; then
    chown -R "$HOST_UID:${HOST_GID:-$HOST_UID}" "$DEPS" ${BUILD_DIR:+"$BUILD_DIR"} ${SCCACHE_DIR:+"$SCCACHE_DIR"} \
      2>/dev/null || true
  fi
}
trap give_back EXIT

# Build tools only (flex and bison for PostgreSQL's libpq): nothing here becomes a
# runtime dependency.
dnf install -y -q --setopt=install_weak_deps=False \
  ninja-build perl-IPC-Cmd perl-Pod-Html perl-Time-Piece flex bison >/dev/null

# The dependencies build-deps.sh names (in CI: one per call, each cached on its own); the
# static OpenSSL first, as DuckDB's extensions link it.
build_deps() {
  server/scripts/build-deps.sh "$@"
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
  # The stats, and the server stopped so its pending writes reach the cache directory.
  if command -v sccache >/dev/null 2>&1; then sccache --show-stats || true; sccache --stop-server >/dev/null || true; fi
  server/scripts/check-glibc-baseline.sh "$build_dir/hardcharger-server" "$build_dir/hardcharger" \
    "$DEPS"/duckdb-install/lib/libduckdb.so
}

case "$MODE" in
  deps) build_deps "$@" ;;
  server) build_server "$@" ;;
  # More targets of a configured build dir: build <build dir> [cmake --build args...]
  build) BUILD_DIR="${1:?build dir}"; cmake --build "$@" ;;
  *) echo "unknown mode $MODE (deps <dependency...> | server <build dir> [cmake args...] | build <build dir> [args...])" >&2; exit 2 ;;
esac
