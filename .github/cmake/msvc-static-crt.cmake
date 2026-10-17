# For the Windows CI's dependency builds (windows.yml sets CMAKE_TOOLCHAIN_FILE to this
# file, so every CMake configure in a step reads it, ExternalProject ones included):
# MSVC's static C runtime (/MT), as DuckDB always builds with it, so Arrow (and its
# bundled zstd/lz4) and replxx link with it.
set(CMAKE_POLICY_DEFAULT_CMP0091 NEW)
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
