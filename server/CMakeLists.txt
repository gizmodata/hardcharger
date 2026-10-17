cmake_minimum_required(VERSION 3.28)
project(hardcharger VERSION 0.1.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

include(FetchContent)

# DuckDB 2.0 and Arrow C++ (IPC only), built by scripts/build-deps.sh.
set(HARDCHARGER_DEPS "${CMAKE_CURRENT_SOURCE_DIR}/.deps" CACHE PATH "Pinned dependency builds")
set(WARPSQL_ARROW_PREFIX "${HARDCHARGER_DEPS}/arrow-install" CACHE PATH "" FORCE)
set(WARPSQL_DUCKDB_PREFIX "${HARDCHARGER_DEPS}/duckdb-install" CACHE PATH "" FORCE)

# The Warp SQL server SDK and its DuckDB backend.
add_subdirectory(../third_party/warp-sql/cpp warp-sql EXCLUDE_FROM_ALL)

# jwt-cpp 0.7.2 (header-only; Boost.JSON traits).
set(JWT_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(JWT_DISABLE_PICOJSON ON CACHE BOOL "" FORCE)
FetchContent_Declare(
  jwt-cpp
  URL https://github.com/Thalhammer/jwt-cpp/archive/refs/tags/v0.7.2.tar.gz
  DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
FetchContent_MakeAvailable(jwt-cpp)

# yaml-cpp 0.9.0, for the config file.
set(YAML_CPP_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(YAML_CPP_BUILD_TOOLS OFF CACHE BOOL "" FORCE)
set(YAML_CPP_BUILD_CONTRIB OFF CACHE BOOL "" FORCE)
set(YAML_BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
FetchContent_Declare(
  yaml-cpp
  URL https://github.com/jbeder/yaml-cpp/archive/refs/tags/yaml-cpp-0.9.0.tar.gz
  DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
FetchContent_MakeAvailable(yaml-cpp)

configure_file(src/version.hpp.in "${CMAKE_CURRENT_BINARY_DIR}/generated/version.hpp" @ONLY)

# Everything but main() and the license trust anchor, shared by the server, the
# license-test server and the unit tests.
add_library(hardcharger_core STATIC
  src/admission.cc
  src/auth.cc
  src/config.cc
  src/engine.cc
  src/guards.cc
  src/health.cc
  src/logging.cc
  src/metrics.cc
  src/patterns.cc
  src/sessions.cc
  src/settings.cc
  src/sql_functions.cc
  src/sql_text.cc
  src/tokens.cc)
target_include_directories(hardcharger_core PUBLIC src "${CMAKE_CURRENT_BINARY_DIR}/generated")
target_link_libraries(hardcharger_core PUBLIC warpsql_duckdb jwt-cpp::jwt-cpp yaml-cpp::yaml-cpp)
target_compile_options(hardcharger_core PRIVATE -Wall -Wextra)

function(hardcharger_rpath target)
  if(APPLE)
    set_target_properties(${target} PROPERTIES BUILD_RPATH "${WARPSQL_DUCKDB_PREFIX}/lib"
                                               INSTALL_RPATH "@executable_path/../lib")
  else()
    set_target_properties(${target} PROPERTIES BUILD_RPATH "${WARPSQL_DUCKDB_PREFIX}/lib"
                                               INSTALL_RPATH "\$ORIGIN/../lib")
  endif()
endfunction()

# The server. It trusts only GizmoData's license signing key.
add_executable(hardcharger src/main.cc src/license.cc)
target_link_libraries(hardcharger PRIVATE hardcharger_core)
target_compile_options(hardcharger PRIVATE -Wall -Wextra)
hardcharger_rpath(hardcharger)

# The same server with a second license trust anchor read from the environment
# (HARDCHARGER_TEST_LICENSE_PUBLIC_KEY), so the tests can exercise Enterprise features
# with keys they sign themselves. It is never installed, packaged or shipped: a binary
# that trusts any key its environment names would accept forged licenses.
add_executable(hardcharger-license-test src/main.cc src/license.cc)
target_compile_definitions(hardcharger-license-test PRIVATE HARDCHARGER_TEST_LICENSE_ANCHOR=1)
target_link_libraries(hardcharger-license-test PRIVATE hardcharger_core)
target_compile_options(hardcharger-license-test PRIVATE -Wall -Wextra)
hardcharger_rpath(hardcharger-license-test)

# Unit tests of components that have no SQL surface of their own (googletest 1.18.0).
# server/tests runs them through test_unit_contracts.py and the per-file wrappers.
set(INSTALL_GTEST OFF CACHE BOOL "" FORCE)
set(BUILD_GMOCK OFF CACHE BOOL "" FORCE)
FetchContent_Declare(
  googletest
  URL https://github.com/google/googletest/archive/refs/tags/v1.18.0.tar.gz
  DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
FetchContent_MakeAvailable(googletest)
add_executable(hardcharger-unit-tests
  unit/admission_test.cc
  unit/guards_test.cc
  unit/ingest_test.cc
  unit/log_level_test.cc
  unit/metrics_test.cc
  unit/patterns_test.cc
  unit/sensitive_paths_test.cc
  unit/sql_text_test.cc
  src/license.cc)
target_link_libraries(hardcharger-unit-tests PRIVATE hardcharger_core GTest::gtest_main)
hardcharger_rpath(hardcharger-unit-tests)

install(TARGETS hardcharger RUNTIME DESTINATION bin)
