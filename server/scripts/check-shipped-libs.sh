#!/usr/bin/env bash
# Fails when a shipped executable or library links anything a user's machine may not have.
#
#   Linux: every NEEDED entry (readelf -d) must be glibc's own (libc, libm, libdl,
#          libpthread, librt, the dynamic loader), the GCC runtime (libstdc++, libgcc_s)
#          or libduckdb.so from the tarball. No libssl/libcrypto: OpenSSL is linked in.
#   macOS: every load command (otool -L) must be under /usr/lib or /System, or
#          @rpath/@executable_path/@loader_path (the tarball's own lib/). Nothing from
#          Homebrew (/opt/homebrew, /usr/local/opt, /usr/local/Cellar) or anywhere else.
#
#   server/scripts/check-shipped-libs.sh FILE...
set -euo pipefail
[ "$#" -gt 0 ] || { echo "usage: $0 FILE..." >&2; exit 2; }
status=0
for file in "$@"; do
  [ -L "$file" ] && continue  # a symlink to a library checked under its own name
  case "$(uname)" in
    Linux)
      needed=$(readelf -d "$file" | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p')
      bad=$(printf '%s\n' "$needed" | grep -vE '^(libc\.so\.6|libm\.so\.6|libdl\.so\.2|libpthread\.so\.0|librt\.so\.1|ld-linux[-a-z0-9_]*\.so\.[0-9]+|libstdc\+\+\.so\.6|libgcc_s\.so\.1|libduckdb\.so)$' || true)
      echo "$file: links $(printf '%s' "$needed" | tr '\n' ' ')"
      ;;
    Darwin)
      libs=$(otool -L "$file" | tail -n +2 | awk '{print $1}')
      bad=$(printf '%s\n' "$libs" | grep -vE '^(/usr/lib/|/System/Library/|@rpath/|@executable_path/|@loader_path/)' || true)
      echo "$file: links $(printf '%s' "$libs" | tr '\n' ' ')"
      ;;
    *) echo "check-shipped-libs: unsupported system $(uname)" >&2; exit 2 ;;
  esac
  if [ -n "$bad" ]; then
    echo "ERROR: $file links what the system may not have: $(printf '%s' "$bad" | tr '\n' ' ')" >&2
    status=1
  fi
done
exit $status
