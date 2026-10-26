# Never shipped. DuckDB's public extension modules are not TSan-instrumented: their
# invisible atomics otherwise produce false races in the instrumented engine.
if(NOT CMAKE_CXX_FLAGS MATCHES "-fsanitize=thread")
  message(FATAL_ERROR "The extension fixture is only for ThreadSanitizer builds")
endif()
# Reuse the pinned extension sources and static dependency linkage from iOS, but keep
# native extension loading capabilities. Spatial still uses the signed runtime module.
include("${CMAKE_CURRENT_LIST_DIR}/duckdb-extensions-ios.cmake")
set(DUCKDB_CAPABILITIES "httplib;loadable_extensions" CACHE STRING "" FORCE)
