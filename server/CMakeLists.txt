cmake_minimum_required(VERSION 3.28)
project(hardcharger VERSION 0.2.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

include(FetchContent)

# DuckDB 2.0 and Arrow C++ (IPC only), built by scripts/build-deps.sh.
set(HARDCHARGER_DEPS "${CMAKE_CURRENT_SOURCE_DIR}/.deps" CACHE PATH "Pinned dependency builds")
set(WARPSQL_ARROW_PREFIX "${HARDCHARGER_DEPS}/arrow-install" CACHE PATH "" FORCE)
set(WARPSQL_DUCKDB_PREFIX "${HARDCHARGER_DEPS}/duckdb-install" CACHE PATH "" FORCE)

# The Warp SQL server SDK and its DuckDB backend, and its client library (for the
# command-line client). Its own tests (warpsql-client-tests) build on request only.
set(WARPSQL_BUILD_TESTS ON)
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

# Access control (role-based). The one part of Hardcharger that uses DuckDB's
# internal C++ headers (the plan check reads DuckDB's logical plans); the rest of the
# server includes only src/authz/authz.hpp. It must be compiled exactly as libduckdb was
# (Release, no DuckDB debug or safety switches): its symbols are mangled with DuckDB's
# template arguments, so a mismatch is undefined behaviour, not a link error.
find_package(OpenSSL 3 REQUIRED)
# An object library: its objects go into hardcharger_core, as it and the rest of the
# server call each other (logging, audit), which static libraries linked in one order
# can't express for GNU ld.
add_library(hardcharger_authz OBJECT
  src/authz/authz.cc
  src/authz/ddl.cc
  src/authz/enforcement.cc
  src/authz/engine.cc
  src/authz/gate.cc
  src/authz/model.cc
  src/authz/opa.cc
  src/authz/password.cc
  src/authz/policies.cc
  src/authz/resolver.cc
  src/authz/store.cc
  src/authz/types.cc
  src/authz/views.cc)
target_include_directories(hardcharger_authz PRIVATE src)
target_include_directories(hardcharger_authz SYSTEM PRIVATE
                           "${WARPSQL_DUCKDB_PREFIX}/include/duckdb_third_party")
target_link_libraries(hardcharger_authz PUBLIC warpsql_duckdb OpenSSL::Crypto)
target_compile_options(hardcharger_authz PRIVATE -Wall -Wextra -Wno-unused-parameter)

# Everything but main(), shared by the server and the unit tests.
add_library(hardcharger_core STATIC
  src/admission.cc
  src/audit.cc
  src/auth.cc
  src/catalog_log.cc
  src/config.cc
  src/engine.cc
  src/guards.cc
  src/health.cc
  src/http_client.cc
  src/instrumentation.cc
  src/jwks.cc
  src/logging.cc
  src/metrics.cc
  src/oauth.cc
  src/patterns.cc
  src/sessions.cc
  src/settings.cc
  src/sql_functions.cc
  src/sql_text.cc
  src/tokens.cc)
target_include_directories(hardcharger_core PUBLIC src "${CMAKE_CURRENT_BINARY_DIR}/generated")
target_sources(hardcharger_core PRIVATE $<TARGET_OBJECTS:hardcharger_authz>)
target_link_libraries(hardcharger_core PUBLIC warpsql_duckdb OpenSSL::Crypto jwt-cpp::jwt-cpp yaml-cpp::yaml-cpp)
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

add_executable(hardcharger-server src/main.cc)
target_link_libraries(hardcharger-server PRIVATE hardcharger_core)
target_compile_options(hardcharger-server PRIVATE -Wall -Wextra)
hardcharger_rpath(hardcharger-server)

# Unit tests of components that have no SQL surface of their own (googletest 1.18.0).
# server/tests runs them through the per-file wrappers.
set(INSTALL_GTEST OFF CACHE BOOL "" FORCE)
set(BUILD_GMOCK OFF CACHE BOOL "" FORCE)
FetchContent_Declare(
  googletest
  URL https://github.com/google/googletest/archive/refs/tags/v1.18.0.tar.gz
  DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
FetchContent_MakeAvailable(googletest)
add_executable(hardcharger-unit-tests
  unit/admission_test.cc
  unit/catalog_log_test.cc
  unit/guards_test.cc
  unit/http_client_test.cc
  unit/ingest_test.cc
  unit/instrumentation_test.cc
  unit/log_level_test.cc
  unit/metrics_test.cc
  unit/patterns_test.cc
  unit/redaction_test.cc
  unit/sensitive_paths_test.cc
  unit/sql_text_test.cc)
target_link_libraries(hardcharger-unit-tests PRIVATE hardcharger_core GTest::gtest_main)
hardcharger_rpath(hardcharger-unit-tests)

# The command-line client, `hardcharger` (client/), on the Warp SQL client library, with
# replxx for line editing (built by scripts/build-replxx.sh at its pinned release).
set(HARDCHARGER_REPLXX_PREFIX "${HARDCHARGER_DEPS}/replxx-install" CACHE PATH "replxx install prefix")
find_package(Threads REQUIRED)  # replxx's config names Threads::Threads
find_package(replxx CONFIG REQUIRED PATHS "${HARDCHARGER_REPLXX_PREFIX}" NO_DEFAULT_PATH)
add_library(hardcharger_client STATIC
  client/client_config.cc
  client/command_processor.cc
  client/oauth_flow.cc
  client/output_renderer.cc
  client/pager.cc
  client/password_prompt.cc
  client/platform.cc
  client/shell_completer.cc
  client/shell_loop.cc
  client/sql_processor.cc
  client/syntax_highlighter.cc
  client/warp_connection.cc)
target_include_directories(hardcharger_client PUBLIC client "${CMAKE_CURRENT_BINARY_DIR}/generated")
target_link_libraries(hardcharger_client PUBLIC warpsql_client replxx::replxx)
target_compile_options(hardcharger_client PRIVATE -Wall -Wextra)
add_executable(hardcharger-client client/main.cc)
set_target_properties(hardcharger-client PROPERTIES OUTPUT_NAME hardcharger)
target_link_libraries(hardcharger-client PRIVATE hardcharger_client)
target_compile_options(hardcharger-client PRIVATE -Wall -Wextra)

# The client's tests: GizmoSQL 1.x's client test files, ported (client/tests/). They run
# against a real hardcharger-server, which server/tests starts and names in
# HARDCHARGER_TEST_CLIENT_* variables (test_interactive_client.py and friends).
add_executable(hardcharger-client-tests
  client/tests/client_config_test.cc
  client/tests/interactive_client_test.cc)
target_link_libraries(hardcharger-client-tests PRIVATE hardcharger_client GTest::gtest_main)
target_compile_definitions(hardcharger-client-tests PRIVATE
                           HARDCHARGER_CLIENT_BINARY="$<TARGET_FILE:hardcharger-client>")
add_dependencies(hardcharger-client-tests hardcharger-client)

install(TARGETS hardcharger-server hardcharger-client RUNTIME DESTINATION bin)
