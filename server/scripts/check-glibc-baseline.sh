#!/usr/bin/env bash
# Checks that each file (an executable or shared library) runs on glibc 2.28: prints the
# highest GLIBC_ and GLIBCXX_ symbol versions it needs and the libraries it links, and
# fails when it needs more than glibc 2.28 / EL8's libstdc++ (GLIBCXX_3.4.25) or links
# OpenSSL dynamically (the portable build links it statically).
#
#   server/scripts/check-glibc-baseline.sh FILE...
set -euo pipefail
[ "$#" -gt 0 ] || { echo "usage: $0 FILE..." >&2; exit 2; }
status=0
for file in "$@"; do
  glibc=$(objdump -T "$file" | grep -oE 'GLIBC_2\.[0-9]+(\.[0-9]+)?' | sort -Vu | tail -1 || true)
  cxx=$(objdump -T "$file" | grep -oE 'GLIBCXX_3\.4\.[0-9]+' | sort -Vu | tail -1 || true)
  needed=$(objdump -p "$file" | awk '/NEEDED/ {print $2}' | tr '\n' ' ')
  echo "$file: highest ${glibc:-none}, ${cxx:-no GLIBCXX}; links ${needed:-nothing}"
  if [ -z "$glibc" ] || [ "$(printf '%s\n%s\n' GLIBC_2.28 "$glibc" | sort -V | tail -1)" != GLIBC_2.28 ]; then
    echo "ERROR: $file needs ${glibc:-no GLIBC_ version (not a dynamic glibc file?)}, not within glibc 2.28" >&2
    status=1
  fi
  if [ -n "$cxx" ] && [ "$(printf '%s\n%s\n' GLIBCXX_3.4.25 "$cxx" | sort -V | tail -1)" != GLIBCXX_3.4.25 ]; then
    echo "ERROR: $file needs $cxx, above EL8's libstdc++ (GLIBCXX_3.4.25)" >&2
    status=1
  fi
  case " $needed " in *" libssl"* | *" libcrypto"*)
    echo "ERROR: $file links OpenSSL dynamically" >&2
    status=1 ;;
  esac
done
exit $status
