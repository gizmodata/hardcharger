cmake_minimum_required(VERSION 3.28)
project(hardcharger VERSION 2.1.3 LANGUAGES C CXX)
# The pre-release part of the version, after PROJECT_VERSION (which takes numbers only): ""
# for a release (tag vX.Y.Z), "-rc.N" for a release candidate (tag vX.Y.Z-rc.N). Every
# reported version is HARDCHARGER_VERSION, the two together. .github/scripts/release.sh
# (the release jobs, the MSI) and ui/scripts/version.mjs read this line as written.
set(HARDCHARGER_VERSION_SUFFIX "")
if(HARDCHARGER_VERSION_SUFFIX STREQUAL "")
  set(HARDCHARGER_VERSION_TWEAK 0)
elseif(HARDCHARGER_VERSION_SUFFIX MATCHES "^-rc\\.([1-9][0-9]*)$" AND CMAKE_MATCH_1 LESS_EQUAL 65535)
  # Windows' numeric FILEVERSION/PRODUCTVERSION carry N in their fourth field.
  set(HARDCHARGER_VERSION_TWEAK "${CMAKE_MATCH_1}")
else()
  message(FATAL_ERROR "HARDCHARGER_VERSION_SUFFIX is \"${HARDCHARGER_VERSION_SUFFIX}\": use \"\" or \"-rc.N\" (N 1-65535)")
endif()
set(HARDCHARGER_VERSION "${PROJECT_VERSION}${HARDCHARGER_VERSION_SUFFIX}")

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

include(FetchContent)

# MSVC: the static C runtime (/MT) for everything, as DuckDB always builds with it.
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

# Warnings, as each compiler spells them.
if(MSVC)
  set(HARDCHARGER_WARNINGS /W3)
  set(HARDCHARGER_AUTHZ_WARNINGS /W3)
else()
  set(HARDCHARGER_WARNINGS -Wall -Wextra)
  set(HARDCHARGER_AUTHZ_WARNINGS -Wall -Wextra -Wno-unused-parameter)
endif()

# DuckDB 2.0 and Arrow C++ (IPC only), built by scripts/build-deps.sh.
set(HARDCHARGER_DEPS "${CMAKE_CURRENT_SOURCE_DIR}/.deps" CACHE PATH "Pinned dependency builds")
set(WARPSQL_ARROW_PREFIX "${HARDCHARGER_DEPS}/arrow-install" CACHE PATH "" FORCE)
set(WARPSQL_DUCKDB_PREFIX "${HARDCHARGER_DEPS}/duckdb-install" CACHE PATH "" FORCE)

# HARDCHARGER_STATIC (the release builds): DuckDB, with its built-in extensions, links in
# statically, as Arrow, Boost, yaml-cpp, replxx and (with OPENSSL_USE_STATIC_LIBS) OpenSSL
# always do. The binaries then need only the C/C++ runtime and, on macOS, OS frameworks.
# Otherwise they link libduckdb's shared library (the sanitizer and developer builds).
option(HARDCHARGER_STATIC "Link DuckDB statically (release builds)" OFF)
# HARDCHARGER_EMBEDDED: the server as a library an app runs in its own process
# (hardcharger_embedded, embedded/hardcharger_embedded.h), as the iOS app does (ios/,
# ios/scripts/build-server-ios.sh, which cross-compiles with ios/cmake/ios.toolchain.cmake).
# It builds that library and its smoke test instead of the programs and their tests.
option(HARDCHARGER_EMBEDDED "Build the server as a library for an app (the iOS app)" OFF)
if(HARDCHARGER_STATIC)
  # DuckDB's archives: the extensions first, then its vendored libraries and the engine,
  # with the libraries the extensions link (scripts/build-extension-deps.sh; deps-duckdb.sh
  # keeps copies in the install's lib/static-deps). The engine
  # and the extensions refer to each other, so GNU ld scans the group until it settles.
  # DuckDB's generated loader registers every built-in extension before main() (in a
  # shared libduckdb, the library does that itself); as an interface source, it is
  # compiled into every program that links DuckDB.
  set(_duckdb_lib "${WARPSQL_DUCKDB_PREFIX}/lib")
  if(IOS)
    file(GLOB _duckdb_extensions "${_duckdb_lib}/lib*_extension.a")
  else()
    # Explicitly limit native builds, even if an old install prefix contains extra archives.
    set(_duckdb_extensions)
    foreach(_extension core_functions parquet json icu)
      list(APPEND _duckdb_extensions "${_duckdb_lib}/lib${_extension}_extension.a")
    endforeach()
  endif()
  file(GLOB _duckdb_vendored "${_duckdb_lib}/libduckdb_*.a")
  list(REMOVE_ITEM _duckdb_vendored "${_duckdb_lib}/libduckdb_static.a")
  set(_duckdb_archives ${_duckdb_extensions} ${_duckdb_vendored} "${_duckdb_lib}/libduckdb_static.a")
  if(IOS)
    list(APPEND _duckdb_archives "${_duckdb_lib}/static-deps/libpq.a"
         "${_duckdb_lib}/static-deps/libroaring.a" "${_duckdb_lib}/static-deps/libcurl.a")
  endif()
  foreach(_archive IN LISTS _duckdb_archives)
    if(NOT EXISTS "${_archive}")
      message(FATAL_ERROR "HARDCHARGER_STATIC: ${_archive} is missing (scripts/build-deps.sh duckdb)")
    endif()
  endforeach()
  find_package(OpenSSL 3 REQUIRED)
  find_package(Threads REQUIRED)
  add_library(hardcharger_duckdb INTERFACE)
  target_sources(hardcharger_duckdb INTERFACE
                 "${WARPSQL_DUCKDB_PREFIX}/share/duckdb-static/duckdb_static_extension_loader.c"
                 "${WARPSQL_DUCKDB_PREFIX}/share/duckdb-static/static_extension_autoregister.cpp")
  target_include_directories(hardcharger_duckdb INTERFACE "${WARPSQL_DUCKDB_PREFIX}/include")
  if(IOS)
    # As macOS, with the build's libcurl (in the archives); libcurl also uses the
    # system's proxy settings (SystemConfiguration).
    target_link_libraries(hardcharger_duckdb INTERFACE ${_duckdb_archives} OpenSSL::SSL OpenSSL::Crypto
                          "-framework CoreFoundation" "-framework Security" "-framework SystemConfiguration"
                          Threads::Threads)
  elseif(APPLE)
    # ld64 resolves archives in any order. TLS and system certificate support use OS frameworks.
    target_link_libraries(hardcharger_duckdb INTERFACE ${_duckdb_archives} OpenSSL::SSL OpenSSL::Crypto
                          "-framework CoreFoundation" "-framework Security" Threads::Threads)
  else()
    # Keep OpenSSL inside the archive group for static DuckDB TLS/extension downloads.
    target_link_libraries(hardcharger_duckdb INTERFACE
                          "$<LINK_GROUP:RESCAN,${_duckdb_archives},${OPENSSL_SSL_LIBRARY},${OPENSSL_CRYPTO_LIBRARY}>"
                          OpenSSL::SSL OpenSSL::Crypto Threads::Threads ${CMAKE_DL_LIBS})
  endif()
  # warp-sql links DuckDB through DUCKDB_LIBRARY; find_library leaves a set value alone.
  set(DUCKDB_LIBRARY hardcharger_duckdb CACHE STRING "DuckDB, statically" FORCE)
endif()

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
set(YAML_MSVC_SHARED_RT OFF CACHE BOOL "" FORCE)  # MSVC: the static C runtime, as everything else
FetchContent_Declare(
  yaml-cpp
  URL https://github.com/jbeder/yaml-cpp/archive/refs/tags/yaml-cpp-0.9.0.tar.gz
  DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
FetchContent_MakeAvailable(yaml-cpp)

# The Warp SQL specification's version (the startup banner names it): its OpenAPI
# document's info.version.
file(READ "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/warp-sql/spec/openapi.yaml" _warpsql_openapi LIMIT 4096)
if(NOT _warpsql_openapi MATCHES "\ninfo:\r?\n  title: Warp SQL\r?\n  version: ([0-9]+\\.[0-9]+\\.[0-9]+)\r?\n")
  message(FATAL_ERROR "third_party/warp-sql/spec/openapi.yaml: no info.version for Warp SQL")
endif()
set(WARPSQL_SPEC_VERSION "${CMAKE_MATCH_1}")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
             "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/warp-sql/spec/openapi.yaml")
configure_file(src/version.hpp.in "${CMAKE_CURRENT_BINARY_DIR}/generated/version.hpp" @ONLY)

# The web UI, embedded when HARDCHARGER_WEB_UI names a build of ui/ (cmake/web_ui.cmake).
include(cmake/web_ui.cmake)

# THIRD_PARTY_NOTICES.md, embedded in both binaries for --licenses (cmake/third_party_notices.cmake).
include(cmake/third_party_notices.cmake)

# zlib 1.3.2 (static), which inflates the embedded UI for a client that doesn't take gzip.
set(ZLIB_BUILD_TESTING OFF CACHE BOOL "" FORCE)
set(ZLIB_BUILD_SHARED OFF CACHE BOOL "" FORCE)
set(ZLIB_BUILD_STATIC ON CACHE BOOL "" FORCE)
set(ZLIB_INSTALL OFF CACHE BOOL "" FORCE)
FetchContent_Declare(
  zlib
  URL https://github.com/madler/zlib/releases/download/v1.3.2/zlib-1.3.2.tar.gz
  URL_HASH SHA256=bb329a0a2cd0274d05519d61c667c062e06990d72e125ee2dfa8de64f0119d16
  DOWNLOAD_EXTRACT_TIMESTAMP TRUE
  EXCLUDE_FROM_ALL)
FetchContent_MakeAvailable(zlib)

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
  src/authz/file_guard.cc
  src/authz/gate.cc
  src/authz/introspection.cc
  src/authz/model.cc
  src/authz/opa.cc
  src/authz/password.cc
  src/authz/policies.cc
  src/authz/read_only.cc
  src/authz/resolver.cc
  src/authz/store.cc
  src/authz/types.cc
  src/authz/views.cc)
target_include_directories(hardcharger_authz PRIVATE src)
target_include_directories(hardcharger_authz SYSTEM PRIVATE
                           "${WARPSQL_DUCKDB_PREFIX}/include/duckdb_third_party")
target_link_libraries(hardcharger_authz PUBLIC warpsql_duckdb OpenSSL::Crypto)
target_compile_options(hardcharger_authz PRIVATE ${HARDCHARGER_AUTHZ_WARNINGS})

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
  src/instance_info.cc
  src/instrumentation.cc
  src/jwks.cc
  src/logging.cc
  src/metrics.cc
  src/oauth.cc
  src/patterns.cc
  src/platform.cc
  src/redact.cc
  src/sessions.cc
  src/settings.cc
  src/sql_functions.cc
  src/sql_text.cc
  src/startup_banner.cc
  src/tokens.cc
  src/web_ui.cc
  "${CMAKE_CURRENT_BINARY_DIR}/generated/web_ui_files.cc")
target_include_directories(hardcharger_core PUBLIC src "${CMAKE_CURRENT_BINARY_DIR}/generated")
target_sources(hardcharger_core PRIVATE $<TARGET_OBJECTS:hardcharger_authz>)
target_link_libraries(hardcharger_core PUBLIC warpsql_duckdb OpenSSL::Crypto jwt-cpp::jwt-cpp yaml-cpp::yaml-cpp
                      PRIVATE ZLIB::ZLIBSTATIC)
target_compile_options(hardcharger_core PRIVATE ${HARDCHARGER_WARNINGS})
if(IOS)
  target_compile_definitions(hardcharger_core PRIVATE HARDCHARGER_IOS)
endif()
if(WIN32)
  target_link_libraries(hardcharger_core PUBLIC crypt32 iphlpapi)  # certificate store and local interface addresses
endif()

function(hardcharger_rpath target)
  if(HARDCHARGER_STATIC)
    return()  # nothing to find at run time
  elseif(APPLE)
    set_target_properties(${target} PROPERTIES BUILD_RPATH "${WARPSQL_DUCKDB_PREFIX}/lib"
                                               INSTALL_RPATH "@executable_path/../lib")
  else()
    set_target_properties(${target} PROPERTIES BUILD_RPATH "${WARPSQL_DUCKDB_PREFIX}/lib"
                                               INSTALL_RPATH "\$ORIGIN/../lib")
  endif()
endfunction()

# The command-line client's library (client/), on the Warp SQL client library, with
# replxx for line editing (built by scripts/build-replxx.sh at its pinned release).
set(HARDCHARGER_REPLXX_PREFIX "${HARDCHARGER_DEPS}/replxx-install" CACHE PATH "replxx install prefix")
find_package(Threads REQUIRED)  # replxx's config names Threads::Threads
find_package(replxx CONFIG REQUIRED PATHS "${HARDCHARGER_REPLXX_PREFIX}" NO_DEFAULT_PATH)
add_library(hardcharger_client STATIC
  client/client_config.cc
  client/command_processor.cc
  client/oauth_flow.cc
  client/output_renderer.cc
  client/variant.cc
  client/pager.cc
  client/password_prompt.cc
  client/platform.cc
  client/plain_http_probe.cc
  client/shell_completer.cc
  client/shell_loop.cc
  client/sql_processor.cc
  client/syntax_highlighter.cc
  client/warp_connection.cc)
target_include_directories(hardcharger_client PUBLIC client "${CMAKE_CURRENT_BINARY_DIR}/generated")
target_link_libraries(hardcharger_client PUBLIC warpsql_client replxx::replxx)
target_compile_options(hardcharger_client PRIVATE ${HARDCHARGER_WARNINGS})
if(WIN32)
  # windows.h without GDI: its ERROR macro would replace CommandResult::ERROR.
  target_compile_definitions(hardcharger_client PUBLIC NOGDI)
endif()
if(HARDCHARGER_EMBEDDED)
  # The server for an app's process: main.cc as hardcharger_server_main, with the hooks of
  # src/embedding.hpp, behind embedded/hardcharger_embedded.h. The smoke test starts,
  # uses and stops a server through that API, twice, as an app would.
  file(STRINGS ../third_party/warp-sql/cpp/CMakeLists.txt _warpsql_project REGEX "^project\\(warpsql VERSION ")
  string(REGEX REPLACE "^project\\(warpsql VERSION ([0-9.]+).*" "\\1" HARDCHARGER_WARP_SQL_VERSION "${_warpsql_project}")
  # With the console (embedded/console.cc): the `hardcharger` client's own statement
  # splitting, dot commands and output formatting, for an app's SQL console.
  add_library(hardcharger_embedded STATIC src/main.cc src/embedding.cc embedded/console.cc)
  target_compile_definitions(hardcharger_embedded PRIVATE HARDCHARGER_EMBEDDED main=hardcharger_server_main
                             HARDCHARGER_WARP_SQL_VERSION="${HARDCHARGER_WARP_SQL_VERSION}")
  target_include_directories(hardcharger_embedded PUBLIC embedded)
  target_link_libraries(hardcharger_embedded PUBLIC hardcharger_core hardcharger_notices hardcharger_client)
  target_compile_options(hardcharger_embedded PRIVATE ${HARDCHARGER_WARNINGS})
  if(HARDCHARGER_STATIC)
    target_compile_definitions(hardcharger_embedded PRIVATE HARDCHARGER_STATIC_DUCKDB)
  endif()
  add_executable(hardcharger-embedded-smoke embedded/smoke_test.cc)
  target_link_libraries(hardcharger-embedded-smoke PRIVATE hardcharger_embedded warpsql_client)
  target_compile_options(hardcharger-embedded-smoke PRIVATE ${HARDCHARGER_WARNINGS})
  hardcharger_rpath(hardcharger-embedded-smoke)
  return()
endif()

add_executable(hardcharger-server src/main.cc)
if(WIN32)
  configure_file(src/version.rc.in "${CMAKE_CURRENT_BINARY_DIR}/generated/version.rc" @ONLY)
  set(HARDCHARGER_ICON "${CMAKE_CURRENT_SOURCE_DIR}/../installer/hardcharger.ico")
  configure_file(src/icon.rc.in "${CMAKE_CURRENT_BINARY_DIR}/generated/icon.rc" @ONLY)
  target_sources(hardcharger-server PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/generated/version.rc"
                                            "${CMAKE_CURRENT_BINARY_DIR}/generated/icon.rc")
endif()
target_link_libraries(hardcharger-server PRIVATE hardcharger_core hardcharger_notices)
target_compile_options(hardcharger-server PRIVATE ${HARDCHARGER_WARNINGS})
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
  unit/authority_key_test.cc
  unit/catalog_log_test.cc
  unit/extension_hint_test.cc
  unit/guards_test.cc
  unit/http_client_test.cc
  unit/ingest_test.cc
  unit/instrumentation_test.cc
  unit/log_level_test.cc
  unit/metrics_test.cc
  unit/patterns_test.cc
  unit/platform_test.cc
  unit/redaction_test.cc
  unit/store_test.cc
  unit/sensitive_paths_test.cc
  unit/sql_text_test.cc
  unit/startup_banner_test.cc
  unit/web_ui_test.cc)
target_link_libraries(hardcharger-unit-tests PRIVATE hardcharger_core GTest::gtest_main ZLIB::ZLIBSTATIC)
hardcharger_rpath(hardcharger-unit-tests)
if(MSVC)
  # A PDB with public symbols, so a crash dump of the unit tests names its functions; a
  # link option only, so no object file (or compiler cache entry) changes.
  target_link_options(hardcharger-unit-tests PRIVATE /DEBUG /OPT:REF /OPT:ICF)
endif()

# The command-line client, `hardcharger`, from hardcharger_client (defined above).
add_executable(hardcharger-client client/main.cc)
set_target_properties(hardcharger-client PROPERTIES OUTPUT_NAME hardcharger)
if(WIN32)
  target_sources(hardcharger-client PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/generated/icon.rc")
endif()
target_link_libraries(hardcharger-client PRIVATE hardcharger_client hardcharger_notices)
target_compile_options(hardcharger-client PRIVATE ${HARDCHARGER_WARNINGS})

# The client's tests: GizmoSQL 1.x's client test files, ported (client/tests/). They run
# against a real hardcharger-server, which server/tests starts and names in
# HARDCHARGER_TEST_CLIENT_* variables (test_interactive_client.py and friends).
add_executable(hardcharger-client-tests
  client/tests/client_config_test.cc
  client/tests/variant_test.cc
  client/tests/interactive_client_test.cc)
target_link_libraries(hardcharger-client-tests PRIVATE hardcharger_client GTest::gtest_main)
target_compile_definitions(hardcharger-client-tests PRIVATE
                           HARDCHARGER_CLIENT_BINARY="$<TARGET_FILE:hardcharger-client>"
                           HARDCHARGER_VARIANT_FIXTURES="${CMAKE_CURRENT_SOURCE_DIR}/client/tests/data/variant.json")
add_dependencies(hardcharger-client-tests hardcharger-client)

install(TARGETS hardcharger-server hardcharger-client RUNTIME DESTINATION bin)
