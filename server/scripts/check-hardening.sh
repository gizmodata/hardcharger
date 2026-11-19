#!/usr/bin/env bash
# Fails unless every ELF executable named is built with the hardening server/CMakeLists.txt
# asks for on Linux (HARDCHARGER_HARDENING), read from the file itself with readelf:
#   - a position-independent executable (ELF type DYN with the PIE flag);
#   - full RELRO: a GNU_RELRO segment and immediate binding (BIND_NOW);
#   - a non-executable stack (GNU_STACK without E);
#   - the stack protector (a reference to __stack_chk_fail);
#   - _FORTIFY_SOURCE (a reference to at least one glibc __*_chk function).
#
#   server/scripts/check-hardening.sh dist/hardcharger-linux-amd64/bin/*
set -euo pipefail
[ $# -gt 0 ] || { echo "usage: $0 <ELF file>..." >&2; exit 2; }
command -v readelf >/dev/null || { echo "readelf is needed (binutils)" >&2; exit 2; }

failed=0
for file in "$@"; do
  problems=()
  header=$(readelf -hW "$file") || { echo "$file: not an ELF file" >&2; failed=1; continue; }
  segments=$(readelf -lW "$file")
  dynamic=$(readelf -dW "$file")
  symbols=$(readelf -sW --dyn-syms "$file")
  if ! grep -qE '^\s*Type:\s+DYN' <<<"$header" || ! grep -qE 'FLAGS_1.*\bPIE\b' <<<"$dynamic"; then
    problems+=("not a position-independent executable")
  fi
  grep -q 'GNU_RELRO' <<<"$segments" || problems+=("no RELRO segment")
  if ! grep -qE '\(BIND_NOW\)|FLAGS.*\bBIND_NOW\b|FLAGS_1.*\bNOW\b' <<<"$dynamic"; then
    problems+=("no immediate binding (partial RELRO only)")
  fi
  if ! stack=$(grep 'GNU_STACK' <<<"$segments"); then
    problems+=("no GNU_STACK segment (the stack may be executable)")
  elif grep -qE 'RWE' <<<"$stack"; then
    problems+=("an executable stack")
  fi
  grep -q '__stack_chk_fail' <<<"$symbols" || problems+=("no stack protector (__stack_chk_fail)")
  fortified=$(grep -oE '__[a-z0-9_]+_chk\b' <<<"$symbols" | grep -v '^__stack_chk' | sort -u | tr '\n' ' ' || true)
  [ -n "$fortified" ] || problems+=("no _FORTIFY_SOURCE functions (__*_chk)")
  if [ ${#problems[@]} -gt 0 ]; then
    failed=1
    printf '%s: NOT hardened: %s\n' "$file" "$(IFS=';'; echo "${problems[*]}")" >&2
  else
    echo "$file: PIE, full RELRO, non-executable stack, stack protector, fortified ($fortified)"
  fi
done
exit "$failed"
