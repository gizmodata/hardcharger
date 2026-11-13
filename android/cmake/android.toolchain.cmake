# All native dependencies and the server share one NDK, ABI and API level.
set(ANDROID_ABI "$ENV{HARDCHARGER_ANDROID_ABI}" CACHE STRING "")
if(NOT ANDROID_ABI)
  set(ANDROID_ABI arm64-v8a CACHE STRING "" FORCE)
endif()
set(ANDROID_PLATFORM android-28 CACHE STRING "")
set(ANDROID_STL c++_static CACHE STRING "")
set(CMAKE_POSITION_INDEPENDENT_CODE ON CACHE BOOL "" FORCE)
include("$ENV{ANDROID_NDK_HOME}/build/cmake/android.toolchain.cmake")
if("$ENV{HARDCHARGER_ANDROID_ARROW}" STREQUAL "ON")
  add_compile_definitions(BUILD_TZ_LIB=1)
endif()
# CMake does not declare RESCAN for Android although NDK's lld supports it.
set(CMAKE_LINK_GROUP_USING_RESCAN_SUPPORTED TRUE)
set(CMAKE_LINK_GROUP_USING_RESCAN "LINKER:--start-group" "LINKER:--end-group")
# NDK r28's LLVM crashes compiling Arrow's optional fixed-width SVE paths.
# ARM64's baseline NEON remains enabled; runtime SVE dispatch is unnecessary on Android.
if(ANDROID_ABI STREQUAL "arm64-v8a")
  set(ARROW_RUNTIME_SIMD_LEVEL NONE CACHE STRING "" FORCE)
endif()
# Search our cross-built dependency prefixes without admitting host libraries.
list(APPEND CMAKE_FIND_ROOT_PATH "$ENV{HARDCHARGER_DEPS_DIR}" "$ENV{CHARGEWIRE_DEPS_DIR}")
