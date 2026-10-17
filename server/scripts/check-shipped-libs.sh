#!/usr/bin/env bash
# Fails when a shipped executable links anything a user's machine may not have: the
# release binaries are static but for the C and C++ runtimes and the OS (as GizmoSQL's).
#
#   Linux: every NEEDED entry (readelf -d) must be glibc's own (libc, libm, libdl,
#          libpthread, librt, libresolv, the dynamic loader) or the GCC runtime
#          (libstdc++, libgcc_s), and there must be no RPATH or RUNPATH. No libduckdb,
#          libssl or libcurl: they are linked in.
#   macOS: every dependency (otool -L) must be the OS's, under /usr/lib/ or
#          /System/Library/Frameworks/, and there must be no LC_RPATH. Nothing from
#          Homebrew (/opt/homebrew, /usr/local/opt) or @rpath.
#
#   server/scripts/check-shipped-libs.sh FILE...
set -euo pipefail
[ "$#" -gt 0 ] || { echo "usage: $0 FILE..." >&2; exit 2; }
status=0
for file in "$@"; do
  case "$(uname)" in
    Linux)
      needed=$(readelf -d "$file" | sed -n 's/.*(NEEDED).*\[\(.*\)\]/\1/p')
      bad=$(printf '%s\n' "$needed" | grep -vE '^(libc\.so\.6|libm\.so\.6|libdl\.so\.2|libpthread\.so\.0|librt\.so\.1|libresolv\.so\.2|ld-linux[-a-z0-9_]*\.so\.[0-9]+|libstdc\+\+\.so\.6|libgcc_s\.so\.1)$' || true)
      rpath=$(readelf -d "$file" | grep -E '\((RPATH|RUNPATH)\)' || true)
      [ -z "$rpath" ] || bad="$bad rpath: $rpath"
      echo "$file: links $(printf '%s' "$needed" | tr '\n' ' ')"
      ;;
    Darwin)
      libs=$(otool -L "$file" | tail -n +2 | awk '{print $1}')
      bad=$(printf '%s\n' "$libs" | grep -vE '^(/usr/lib/|/System/Library/Frameworks/)' || true)
      otool -l "$file" | grep -q LC_RPATH && bad="$bad LC_RPATH"
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
