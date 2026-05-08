# ===========================================================================
# rv1106-toolchain.cmake -- CMake toolchain file for Luckfox Pico Ultra
#
# Cross-compile for RV1106G3 (ARM Cortex-A7, uclibc) using the Rockchip
# Buildroot toolchain.
#
# Usage:
#   cmake .. -DCMAKE_TOOLCHAIN_FILE=../cmake/rv1106-toolchain.cmake \
#            -DTOOLCHAIN_DIR=/path/to/arm-rockchip830-linux-uclibcgnueabihf
#
# TOOLCHAIN_DIR:
#   Root of the standalone toolchain. Expected layout:
#     bin/arm-rockchip830-linux-uclibcgnueabihf-gcc
#     arm-rockchip830-linux-uclibcgnueabihf/sysroot/
#
#   Can also be set via environment variable TOOLCHAIN_DIR.
# ===========================================================================

set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR armv7l)

# --------------------------------------------------------------------------
# Resolve toolchain root directory
# --------------------------------------------------------------------------
if(NOT DEFINED TOOLCHAIN_DIR)
    if(DEFINED ENV{TOOLCHAIN_DIR})
        set(TOOLCHAIN_DIR "$ENV{TOOLCHAIN_DIR}")
    elseif(DEFINED ENV{LUCKFOX_SDK_PATH})
        set(TOOLCHAIN_DIR "$ENV{LUCKFOX_SDK_PATH}/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf")
    else()
        message(FATAL_ERROR
            "TOOLCHAIN_DIR not set. Provide -DTOOLCHAIN_DIR=<path> or set "
            "TOOLCHAIN_DIR / LUCKFOX_SDK_PATH environment variable.")
    endif()
endif()

set(CROSS_PREFIX "${TOOLCHAIN_DIR}/bin/arm-rockchip830-linux-uclibcgnueabihf-")

# --------------------------------------------------------------------------
# Compilers
# --------------------------------------------------------------------------
set(CMAKE_C_COMPILER   "${CROSS_PREFIX}gcc")
set(CMAKE_CXX_COMPILER "${CROSS_PREFIX}g++")
set(CMAKE_STRIP        "${CROSS_PREFIX}strip")
set(CMAKE_AR           "${CROSS_PREFIX}ar")
set(CMAKE_RANLIB       "${CROSS_PREFIX}ranlib")

# --------------------------------------------------------------------------
# Sysroot (uclibc standard C library headers + runtime)
# --------------------------------------------------------------------------
set(CMAKE_SYSROOT "${TOOLCHAIN_DIR}/arm-rockchip830-linux-uclibcgnueabihf/sysroot")

# --------------------------------------------------------------------------
# Search path configuration -- only search target sysroot, never host
# --------------------------------------------------------------------------
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
