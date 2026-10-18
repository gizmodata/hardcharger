# For the Windows CI's DuckDB and server builds (windows.yml sets CMAKE_TOOLCHAIN_FILE to
# this file): MSVC's static C runtime (msvc-static-crt.cmake), and vcpkg's static triplet
# ($VCPKG_TRIPLET, e.g. x64-windows-static, in $VCPKG_ROOT) for the libraries DuckDB's
# built-in extensions link: OpenSSL, libpq (the postgres scanner), libcurl (httpfs) and
# CRoaring (ducklake). vcpkg's own find modules give each its complete link interface
# (libpq's pgcommon and pgport, the Windows libraries they need).
include("${CMAKE_CURRENT_LIST_DIR}/msvc-static-crt.cmake")
set(VCPKG_TARGET_TRIPLET "$ENV{VCPKG_TRIPLET}" CACHE STRING "")
set(VCPKG_MANIFEST_MODE OFF CACHE BOOL "")
include("$ENV{VCPKG_ROOT}/scripts/buildsystems/vcpkg.cmake")
