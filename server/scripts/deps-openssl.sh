#!/usr/bin/env bash
# Builds a static OpenSSL 3 (the 3.5 LTS) into server/.deps/openssl-install, which the
# release builds link into everything they ship (server, client, libduckdb's extensions):
# on Linux EL8 ships OpenSSL 1.1.1, on macOS there is no system OpenSSL 3 (Homebrew's is
# not on a user's Mac), and static, the binaries need no libssl at all. Their certificates
# come from the system's CA bundle at run time (each TLS user probes /etc/ssl/cert.pem and
# the distributions' paths), never from OpenSSL's compiled-in directory. The sanitizer
# jobs use the system's OpenSSL 3. `build-deps.sh openssl` runs it.
set -euo pipefail
OPENSSL_VERSION=3.5.9
OPENSSL_SHA256=603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${HARDCHARGER_DEPS_DIR:-$HERE/.deps}/openssl-install"
[ -f "$PREFIX/.openssl-$OPENSSL_VERSION.stamp" ] && exit 0
src=$(mktemp -d)
curl -fsSL -o "$src/openssl.tar.gz" \
  "https://github.com/openssl/openssl/releases/download/openssl-$OPENSSL_VERSION/openssl-$OPENSSL_VERSION.tar.gz"
if command -v sha256sum >/dev/null; then hash=(sha256sum); else hash=(shasum -a 256); fi
sum=$("${hash[@]}" "$src/openssl.tar.gz" | cut -d' ' -f1)
[ "$sum" = "$OPENSSL_SHA256" ] || { echo "openssl.tar.gz: checksum $sum, expected $OPENSSL_SHA256" >&2; exit 1; }
tar -xzf "$src/openssl.tar.gz" -C "$src" --strip-components=1
rm -rf "$PREFIX"
(cd "$src" && ./config no-shared no-tests no-docs --prefix="$PREFIX" --libdir=lib \
   && make -j"${CMAKE_BUILD_PARALLEL_LEVEL:-$(getconf _NPROCESSORS_ONLN)}" build_libs >/dev/null \
   && make install_dev >/dev/null)
rm -rf "$src"
touch "$PREFIX/.openssl-$OPENSSL_VERSION.stamp"
echo "OpenSSL $OPENSSL_VERSION installed in $PREFIX"
