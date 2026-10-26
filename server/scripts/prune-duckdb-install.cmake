# CMake install does not remove archives from an older build. The Windows SDK discovers
# installed extension archives, so delete only the retired optional extension libraries.
if(NOT DUCKDB_INSTALL_DIR)
  message(FATAL_ERROR "DUCKDB_INSTALL_DIR is required")
endif()
foreach(name httpfs postgres_scanner ducklake tpch autocomplete)
  foreach(libdir lib lib64)
    file(REMOVE
      "${DUCKDB_INSTALL_DIR}/${libdir}/lib${name}_extension.a"
      "${DUCKDB_INSTALL_DIR}/${libdir}/${name}_extension.lib")
  endforeach()
endforeach()
