#!/usr/bin/env bash
# Prints the section the Windows release's THIRD_PARTY_NOTICES.md ends with: the libraries
# the Windows builds link from vcpkg (.github/windows-vcpkg-packages.txt and what those
# depend on), in place of the versions the file lists for Linux and macOS, each with its
# version and the copyright file vcpkg installs for it. Fails when vcpkg lists no library
# for the triplet, or one has no copyright file.
#
#   .github/scripts/vcpkg-notices.sh VCPKG_ROOT TRIPLET >> "$stage/THIRD_PARTY_NOTICES.md"
#
# VCPKG_EXE overrides the vcpkg executable (default: VCPKG_ROOT/vcpkg.exe).
set -euo pipefail
root="${1:?usage: $0 VCPKG_ROOT TRIPLET}"
triplet="${2:?usage: $0 VCPKG_ROOT TRIPLET}"
vcpkg="${VCPKG_EXE:-$root/vcpkg.exe}"

listing=$("$vcpkg" list)
# "name:triplet  version  description"; a feature's line is "name[feature]:triplet ...".
ports=()
versions=()
while read -r spec version _; do
  [[ "$spec" == *"["* ]] && continue
  [[ "$spec" == *":$triplet" ]] || continue
  ports+=("${spec%%:*}")
  versions+=("$version")
done <<<"$(tr -d '\r' <<<"$listing")"
[ "${#ports[@]}" -gt 0 ] || { echo "vcpkg-notices: vcpkg lists no library for $triplet" >&2; exit 1; }

cat <<EOF

## Windows: the libraries from vcpkg

The Windows builds link the libraries below from vcpkg (triplet \`$triplet\`, statically) in place
of the versions listed above for Linux and macOS (OpenSSL, libpq, libcurl, CRoaring and what they
depend on). Each is listed with its version and the copyright file vcpkg installs with it.

| Library | Version |
| --- | --- |
EOF
for i in "${!ports[@]}"; do
  printf '| %s | %s |\n' "${ports[$i]}" "${versions[$i]}"
done
for i in "${!ports[@]}"; do
  copyright="$root/installed/$triplet/share/${ports[$i]}/copyright"
  [ -f "$copyright" ] || { echo "vcpkg-notices: ${ports[$i]} has no $copyright" >&2; exit 1; }
  text=$(tr -d '\r' <"$copyright")
  # A fence longer than any run of tildes in the text.
  fence="~~~~"
  while [[ "$text" == *"$fence"* ]]; do fence="$fence~"; done
  printf '\n### %s %s (vcpkg)\n\ncopyright:\n\n%stext\n%s\n%s\n' "${ports[$i]}" "${versions[$i]}" "$fence" "$text" "$fence"
done
