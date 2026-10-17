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

# Access control (role-based, Enterprise). The one part of Hardcharger that uses DuckDB's
# internal C++ headers (the plan check reads DuckDB's logical plans); the rest of the
# server includes only src/authz/authz.hpp. It must be compiled exactly as libduckdb was
# (Release, no DuckDB debug or safety switches): its symbols are mangled with DuckDB's
# template arguments, so a mismatch is undefined behaviour, not a link error.
find_package(OpenSSL 3 REQUIRED)
add_library(hardcharger_authz STATIC
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

# Test builds trust a license key whose private half is in server/tests/license, so the
# access-control tests can run without a GizmoData license. Never for a release.
option(HARDCHARGER_TRUST_TEST_LICENSES "Trust the tests' license signing key (tests only)" OFF)

add_executable(hardcharger
  src/audit.cc
  src/auth.cc
  src/config.cc
  src/engine.cc
  src/license.cc
  src/logging.cc
  src/main.cc
  src/tokens.cc)
target_include_directories(hardcharger PRIVATE src "${CMAKE_CURRENT_BINARY_DIR}/generated")
target_link_libraries(hardcharger PRIVATE hardcharger_authz warpsql_duckdb jwt-cpp::jwt-cpp
                                          yaml-cpp::yaml-cpp)
if(HARDCHARGER_TRUST_TEST_LICENSES)
  file(READ "${CMAKE_CURRENT_SOURCE_DIR}/tests/license/test-license-public.pem" TEST_LICENSE_KEY)
  file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/generated/test_license.hpp"
       "// Generated: the tests' license key (HARDCHARGER_TRUST_TEST_LICENSES).\n"
       "#define HARDCHARGER_TEST_LICENSE_PUBLIC_KEY R\"(${TEST_LICENSE_KEY})\"\n")
  target_compile_definitions(hardcharger PRIVATE HARDCHARGER_TRUST_TEST_LICENSES=1)
endif()
target_compile_options(hardcharger PRIVATE -Wall -Wextra)
if(APPLE)
  set_target_properties(hardcharger PROPERTIES BUILD_RPATH "${WARPSQL_DUCKDB_PREFIX}/lib"
                                               INSTALL_RPATH "@executable_path/../lib")
else()
  set_target_properties(hardcharger PROPERTIES BUILD_RPATH "${WARPSQL_DUCKDB_PREFIX}/lib"
                                               INSTALL_RPATH "\$ORIGIN/../lib")
endif()
install(TARGETS hardcharger RUNTIME DESTINATION bin)
