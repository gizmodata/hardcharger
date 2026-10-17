#!/usr/bin/env bash
# The release's Windows binaries are single files: every dependency (DuckDB and its
# extensions, Arrow, OpenSSL, Boost, yaml-cpp, zstd, replxx) and the C++ runtime (/MT)
# linked in, so they import Windows' own DLLs only. This fails when an executable imports
# any DLL outside ALLOWED: the set GizmoSQL 1.x's Windows release (v1.42.0's
# gizmosql_server.exe and gizmosql_client.exe, x64 and arm64) imports, plus two more
# parts of Windows the server needs:
#   mswsock.dll                       Winsock's extensions: Asio's acceptor calls AcceptEx
#                                     (gRPC, in GizmoSQL, looks it up at run time instead)
#   api-ms-win-core-synch-l1-2-0.dll  WaitOnAddress/WakeByAddress (an API set of
#                                     kernelbase, Windows 8 and later), from the static
#                                     C++ runtime's atomic waits
#
#   check-windows-imports.sh EXE...      (needs dumpbin: run it in a Developer shell)
set -euo pipefail
ALLOWED="advapi32.dll bcrypt.dll crypt32.dll dbghelp.dll iphlpapi.dll kernel32.dll ntdll.dll
rstrtmgr.dll shell32.dll user32.dll ws2_32.dll
mswsock.dll api-ms-win-core-synch-l1-2-0.dll"
[ "$#" -gt 0 ] || { echo "usage: $0 EXE..." >&2; exit 2; }
status=0
for exe in "$@"; do
  # The DLL names dumpbin lists, imports and delay-loaded imports alike.
  dlls=$(dumpbin -nologo -dependents "$exe" | tr -d '\r' |
         sed -n 's/^ *\([A-Za-z0-9_.-]*\.[Dd][Ll][Ll]\) *$/\1/p' | tr '[:upper:]' '[:lower:]' | sort -u)
  if [ -z "$dlls" ]; then
    echo "FAIL: $exe: dumpbin listed no imports" >&2
    status=1
    continue
  fi
  for dll in $dlls; do
    case " $(echo $ALLOWED) " in
      *" $dll "*) echo "ok:   $exe imports $dll" ;;
      *) echo "FAIL: $exe imports $dll, which is not one of the Windows system DLLs allowed" >&2; status=1 ;;
    esac
  done
done
exit $status
