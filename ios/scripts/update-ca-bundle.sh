#!/usr/bin/env bash
# Fetches the CA certificates the app bundles (Hardcharger/Resources/cacert.pem): Mozilla's
# trusted roots as curl's maintainers extract them (https://curl.se/docs/caextract.html),
# at a dated, checksummed release. iOS has no CA bundle for OpenSSL (no /etc/ssl), and the
# server's outbound HTTPS (httpfs, OpenID discovery) and the console's connections use
# OpenSSL, so the app points SSL_CERT_FILE at this file. (The web views and URLSession use
# iOS's own trust store.) Bump DATE and SHA256 together; the file is MPL-2.0 (Mozilla).
set -euo pipefail
DATE=2026-09-25
SHA256=a41b5d356aea97a529fe27e0f7316d2f9d946d75927476cf9cf1b90637d00505
OUT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/Hardcharger/Resources/cacert.pem"
tmp=$(mktemp)
curl -fsSL -o "$tmp" "https://curl.se/ca/cacert-$DATE.pem"
sum=$(shasum -a 256 "$tmp" | cut -d' ' -f1)
[ "$sum" = "$SHA256" ] || { echo "cacert-$DATE.pem: checksum $sum, expected $SHA256" >&2; rm -f "$tmp"; exit 1; }
mv "$tmp" "$OUT"
echo "wrote $OUT ($(grep -c 'BEGIN CERTIFICATE' "$OUT") certificates, Mozilla data of $DATE)"
