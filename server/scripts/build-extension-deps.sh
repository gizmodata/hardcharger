#!/usr/bin/env bash
# Builds the libraries Hardcharger's built-in DuckDB extensions link
# (server/scripts/duckdb-extensions.cmake) into server/.deps, static and position
# independent (they end up inside libduckdb), so the server needs neither at run time:
#
#   CRoaring  ducklake's deletion vectors          -> server/.deps/roaring-install
#   libpq     the postgres scanner (PostgreSQL)    -> server/.deps/libpq-install
#   libcurl   httpfs, on Linux                     -> server/.deps/curl-install
#
# OpenSSL comes from $OPENSSL_ROOT_DIR (or the system). httpfs needs libcurl 7.85 or newer
# (EL8 has 7.61): Linux builds their own, macOS uses the system's. Built without a CA
# bundle path: httpfs finds the system's bundle itself and hands it to curl.
# Each is built once (a stamp names its version); build-deps.sh runs this.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPS="${WARPSQL_DEPS_DIR:-$HERE/.deps}"
JOBS="${CMAKE_BUILD_PARALLEL_LEVEL:-$(getconf _NPROCESSORS_ONLN)}"

ROARING_VERSION=5.2.4
ROARING_SHA256=0047582c0b1ac7a2a950156f0b23b52ad060dbfbd1f082939ffa369014675838
POSTGRES_VERSION=18.6
POSTGRES_SHA256=555610c24d53e4316da5b7d3fc25c279d96856d5e0e23ee308c328c5fa881d9f
CURL_VERSION=8.23.0
CURL_SHA256=8dadd7d2536a314f982ad05a651b713f426bf7d02cc2c76cffb9796f2218e6c5

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }
fetch() {  # url sha256 dir: download, verify, unpack into dir
  curl -fsSL -o "$3.tar" "$1"
  [ "$(sha256 "$3.tar")" = "$2" ] || { echo "checksum mismatch for $1" >&2; exit 1; }
  mkdir -p "$3" && tar -xf "$3.tar" -C "$3" --strip-components=1 && rm "$3.tar"
}

mkdir -p "$DEPS"
ROARING="$DEPS/roaring-install"
if [ ! -f "$ROARING/.roaring-$ROARING_VERSION.stamp" ]; then
  src=$(mktemp -d)
  fetch "https://github.com/RoaringBitmap/CRoaring/archive/refs/tags/v$ROARING_VERSION.tar.gz" \
    "$ROARING_SHA256" "$src/roaring"
  rm -rf "$ROARING"
  cmake -S "$src/roaring" -B "$src/build" -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$ROARING" -DCMAKE_INSTALL_LIBDIR=lib -DBUILD_SHARED_LIBS=OFF \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DENABLE_ROARING_TESTS=OFF -DROARING_USE_CPM=OFF >/dev/null
  cmake --build "$src/build" >/dev/null
  cmake --install "$src/build" >/dev/null
  rm -rf "$src"
  touch "$ROARING/.roaring-$ROARING_VERSION.stamp"
fi

LIBPQ="$DEPS/libpq-install"
if [ ! -f "$LIBPQ/.libpq-$POSTGRES_VERSION.stamp" ]; then
  src=$(mktemp -d)
  fetch "https://ftp.postgresql.org/pub/source/v$POSTGRES_VERSION/postgresql-$POSTGRES_VERSION.tar.bz2" \
    "$POSTGRES_SHA256" "$src/postgres"
  rm -rf "$LIBPQ"
  ssl_flags=()
  if [ -n "${OPENSSL_ROOT_DIR:-}" ]; then
    ssl_flags=(--with-includes="$OPENSSL_ROOT_DIR/include" --with-libraries="$OPENSSL_ROOT_DIR/lib")
  fi
  (cd "$src/postgres" \
     && CFLAGS="-O2 -fPIC" ./configure --prefix="$LIBPQ" --with-ssl=openssl "${ssl_flags[@]}" \
          --without-readline --without-icu --without-zlib --without-zstd --without-lz4 \
          --without-gssapi --without-ldap >/dev/null \
     && make -s -j"$JOBS" -C src/include >/dev/null \
     && make -s -j"$JOBS" -C src/common >/dev/null \
     && make -s -j"$JOBS" -C src/port >/dev/null \
     && make -s -j"$JOBS" -C src/interfaces/libpq all-static-lib >/dev/null \
     && make -s -C src/include install >/dev/null)
  # The static library only (no shared one for CMake's FindPostgreSQL to prefer), as one
  # archive that also carries the PostgreSQL common and port code it calls (libpq.a alone
  # leaves those to the shared library's link).
  mkdir -p "$LIBPQ/lib"
  cp "$src/postgres/src/interfaces/libpq/libpq-fe.h" "$src/postgres/src/interfaces/libpq/libpq-events.h" \
    "$LIBPQ/include/"
  cp "$src/postgres/src/interfaces/libpq/libpq.a" "$LIBPQ/lib/libpq.a"
  combined="$src/libpq-combined.a"
  if [ "$(uname)" = Darwin ]; then
    libtool -static -o "$combined" "$LIBPQ/lib/libpq.a" \
      "$src/postgres/src/common/libpgcommon_shlib.a" "$src/postgres/src/port/libpgport_shlib.a"
  else
    printf 'create %s\naddlib %s\naddlib %s\naddlib %s\nsave\nend\n' "$combined" "$LIBPQ/lib/libpq.a" \
      "$src/postgres/src/common/libpgcommon_shlib.a" "$src/postgres/src/port/libpgport_shlib.a" | ar -M
  fi
  mv "$combined" "$LIBPQ/lib/libpq.a"
  rm -rf "$src"
  touch "$LIBPQ/.libpq-$POSTGRES_VERSION.stamp"
fi
CURL="$DEPS/curl-install"
if [ "$(uname)" != Darwin ] && [ ! -f "$CURL/.curl-$CURL_VERSION.stamp" ]; then
  src=$(mktemp -d)
  fetch "https://curl.se/download/curl-$CURL_VERSION.tar.xz" "$CURL_SHA256" "$src/curl"
  rm -rf "$CURL"
  ssl_root=()
  if [ -n "${OPENSSL_ROOT_DIR:-}" ]; then ssl_root=(-DOPENSSL_ROOT_DIR="$OPENSSL_ROOT_DIR" -DOPENSSL_USE_STATIC_LIBS=TRUE); fi
  cmake -S "$src/curl" -B "$src/build" -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$CURL" -DCMAKE_INSTALL_LIBDIR=lib -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -DBUILD_SHARED_LIBS=OFF -DBUILD_STATIC_LIBS=ON -DBUILD_CURL_EXE=OFF -DBUILD_LIBCURL_DOCS=OFF \
    -DBUILD_MISC_DOCS=OFF -DENABLE_CURL_MANUAL=OFF -DBUILD_TESTING=OFF -DCURL_USE_OPENSSL=ON "${ssl_root[@]}" \
    -DCURL_CA_BUNDLE=none -DCURL_CA_PATH=none -DCURL_DISABLE_LDAP=ON -DCURL_USE_LIBPSL=OFF \
    -DCURL_USE_LIBSSH2=OFF -DUSE_LIBIDN2=OFF -DUSE_NGHTTP2=OFF -DCURL_BROTLI=OFF -DCURL_ZSTD=OFF \
    -DCURL_ZLIB=OFF >/dev/null
  cmake --build "$src/build" >/dev/null
  cmake --install "$src/build" >/dev/null
  rm -rf "$src"
  touch "$CURL/.curl-$CURL_VERSION.stamp"
fi
echo "extension libraries built in $DEPS"
