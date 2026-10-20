# CMake toolchain for Hardcharger's iOS builds (ios/scripts/build-server-ios.sh).
#
# The SDK comes from the environment, not a -D option, so CMake's try_compile projects and
# every ExternalProject a dependency starts (Arrow's bundled zstd and lz4, say) see the same
# target as the build that includes them:
#
#   HARDCHARGER_IOS_SDK=iphoneos         devices (arm64)
#   HARDCHARGER_IOS_SDK=iphonesimulator  the simulator on Apple silicon (arm64)
#   HARDCHARGER_IOS_DEPLOYMENT_TARGET    the oldest iOS the objects run on (default 18.0)
#
# Besides CMake's own iOS settings, the compilers get an explicit -target triple: a
# sub-build that forwards only CMAKE_<LANG>_FLAGS still compiles for iOS, and an object
# built for macOS by mistake fails the link with "building for iOS, but linking in object
# file built for macOS" instead of slipping in (ios/scripts/check-platform.sh checks
# every archive too).

set(_sdk "$ENV{HARDCHARGER_IOS_SDK}")
if(NOT _sdk STREQUAL "iphoneos" AND NOT _sdk STREQUAL "iphonesimulator")
  message(FATAL_ERROR "ios.toolchain.cmake: set HARDCHARGER_IOS_SDK to iphoneos or iphonesimulator (it is '${_sdk}')")
endif()
set(_min "$ENV{HARDCHARGER_IOS_DEPLOYMENT_TARGET}")
if(_min STREQUAL "")
  set(_min "18.0")
endif()

set(CMAKE_SYSTEM_NAME iOS)
set(CMAKE_SYSTEM_PROCESSOR arm64)
set(CMAKE_OSX_ARCHITECTURES arm64 CACHE STRING "" FORCE)
set(CMAKE_OSX_DEPLOYMENT_TARGET "${_min}" CACHE STRING "" FORCE)
execute_process(COMMAND xcrun --sdk ${_sdk} --show-sdk-path OUTPUT_VARIABLE _sysroot
                OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
set(CMAKE_OSX_SYSROOT "${_sysroot}" CACHE PATH "" FORCE)

if(_sdk STREQUAL "iphonesimulator")
  set(_triple "arm64-apple-ios${_min}-simulator")
else()
  set(_triple "arm64-apple-ios${_min}")
endif()
set(CMAKE_C_FLAGS_INIT "-target ${_triple}")
set(CMAKE_CXX_FLAGS_INIT "-target ${_triple}")
set(CMAKE_ASM_FLAGS_INIT "-target ${_triple}")
set(CMAKE_C_COMPILER_TARGET "${_triple}")
set(CMAKE_CXX_COMPILER_TARGET "${_triple}")

# Static libraries only: everything is linked into the app.
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# Host programs (perl, python, git) from the host; libraries and headers from the SDK or
# the prefixes the build names.
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY BOTH)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE BOTH)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE BOTH)
