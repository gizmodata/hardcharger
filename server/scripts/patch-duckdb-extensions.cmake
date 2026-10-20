# Hardcharger's own patches to the out-of-tree extensions built with APPLY_PATCHES
# (duckdb-extensions.cmake). DuckDB's build applies every .patch in
# <duckdb>/.github/patches/extensions/<name>/, in name order, to the extension's checkout
# (<duckdb>/scripts/apply_extension_patches.py, `patch -p1`). This copies
# extension-patches/<name>/*.patch there, next to DuckDB's own patches for that extension.
# Ours are named hardcharger-*, which sorts after DuckDB's numbered ones, and copies left
# by an earlier run are removed first, so a renamed or deleted patch doesn't linger.
#
#   extension-patches/ducklake/hardcharger-keep-metadata-storage-version.patch
#       An existing DuckDB metadata file keeps its own storage version, so a DuckLake that
#       DuckDB 1.5 created stays readable by DuckDB 1.5 after the server writes to it
#       (upstream attaches it with STORAGE_VERSION 'latest', which upgrades the file).
#
# DuckDB's build patches an extension only when it fetches it, so a build tree that
# fetched the extension with other patches than these (a stamp of their hash, in
# <duckdb-build>/_deps) has that checkout removed here, and the build fetches and patches
# it again. deps-duckdb.sh checks after the build that each patch reached the source.
#
# Run as: cmake -DDUCKDB_SOURCE_DIR=<dir> [-DDUCKDB_BUILD_DIR=<dir>] -P patch-duckdb-extensions.cmake
# (DUCKDB_BUILD_DIR: warp-sql's build-duckdb.sh's, <DUCKDB_SOURCE_DIR>/../duckdb-build.)

if(NOT DUCKDB_BUILD_DIR)
  get_filename_component(DUCKDB_BUILD_DIR "${DUCKDB_SOURCE_DIR}/../duckdb-build" ABSOLUTE)
endif()

file(GLOB extensions LIST_DIRECTORIES true "${CMAKE_CURRENT_LIST_DIR}/extension-patches/*")
foreach(dir IN LISTS extensions)
  if(NOT IS_DIRECTORY "${dir}")
    continue()
  endif()
  get_filename_component(name "${dir}" NAME)
  set(target "${DUCKDB_SOURCE_DIR}/.github/patches/extensions/${name}")
  file(GLOB stale "${target}/hardcharger-*.patch")
  if(stale)
    file(REMOVE ${stale})
  endif()
  file(GLOB all "${dir}/*")
  file(GLOB patches "${dir}/hardcharger-*.patch")
  list(LENGTH all n_all)
  list(LENGTH patches n_patches)
  if(NOT n_all EQUAL n_patches OR n_patches EQUAL 0)
    message(FATAL_ERROR "patch-duckdb-extensions: ${dir} must hold only hardcharger-*.patch files")
  endif()
  file(MAKE_DIRECTORY "${target}")
  file(COPY ${patches} DESTINATION "${target}")

  set(sums "")
  foreach(patch IN LISTS patches)
    file(SHA256 "${patch}" sum)
    get_filename_component(patch_name "${patch}" NAME)
    string(APPEND sums "${patch_name} ${sum}\n")
  endforeach()
  set(stamp "${DUCKDB_BUILD_DIR}/_deps/.hardcharger-${name}-patches")
  set(previous "")
  if(EXISTS "${stamp}")
    file(READ "${stamp}" previous)
  endif()
  if(NOT previous STREQUAL sums)
    file(GLOB fetched LIST_DIRECTORIES true "${DUCKDB_BUILD_DIR}/_deps/${name}_extension_fc-*")
    if(fetched)
      message(STATUS "patch-duckdb-extensions: ${name}'s patches changed; it is fetched again")
      file(REMOVE_RECURSE ${fetched})
    endif()
    file(WRITE "${stamp}" "${sums}")
  endif()
  foreach(patch IN LISTS patches)
    get_filename_component(patch_name "${patch}" NAME)
    message(STATUS "patch-duckdb-extensions: ${name} gets ${patch_name}")
  endforeach()
endforeach()
