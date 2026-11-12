# Match iOS's bundled extension set and pinned sources. Local Android builds retain
# the repository's non-iOS automatic loading and signature-verification defaults.
include("${CMAKE_CURRENT_LIST_DIR}/duckdb-extensions-ios.cmake")
set(DUCKDB_CAPABILITIES "httplib;loadable_extensions" CACHE STRING "" FORCE)
