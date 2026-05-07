# ===========================================================================
# CMakeLists.txt -- AI-HUD Camera Display Pipeline
#
# Cross-compile for Luckfox Pico Ultra (RV1106G3) using Buildroot toolchain
#
# Usage:
#   export LUCKFOX_SDK_PATH=/path/to/luckfox-pico
#   mkdir build && cd build
#   cmake .. \
#     -DCMAKE_C_COMPILER=${LUCKFOX_SDK_PATH}/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin/arm-rockchip830-linux-uclibcgnueabihf-gcc \
#     -DSYSROOT=${LUCKFOX_SDK_PATH}/sysdrv/out/rootfs_uclibc_rv1106
#   make -j$(nproc)
#
# Deploy:
#   scp ai-hud root@172.32.0.93:/root/
# ===========================================================================

cmake_minimum_required(VERSION 3.10)
project(ai-hud C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

# --------------------------------------------------------------------------
# SDK / Sysroot paths
#
# SYSROOT should be set to the Buildroot output sysroot that contains
# the RKMPI headers and libraries. Adjust to match your SDK layout.
# Typical path: ${LUCKFOX_SDK_PATH}/sysdrv/out/rootfs_uclibc_rv1106
# --------------------------------------------------------------------------
if(NOT DEFINED SYSROOT)
    if(DEFINED ENV{LUCKFOX_SDK_PATH})
        set(SYSROOT "$ENV{LUCKFOX_SDK_PATH}/sysdrv/out/rootfs_uclibc_rv1106"
            CACHE PATH "Sysroot for cross compilation")
    else()
        set(SYSROOT "/opt/luckfox-sysroot"
            CACHE PATH "Sysroot for cross compilation (set -DSYSROOT=... or LUCKFOX_SDK_PATH env)")
    endif()
endif()

message(STATUS "SYSROOT = ${SYSROOT}")

# Derive RKMPI include/lib paths from sysroot
set(RKMPI_INC_DIR "${SYSROOT}/usr/include" CACHE PATH "RKMPI header directory")
set(RKMPI_LIB_DIR "${SYSROOT}/usr/lib"     CACHE PATH "RKMPI library directory")

# --------------------------------------------------------------------------
# Compiler flags
# --------------------------------------------------------------------------
set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -Wall -Wextra -Wno-unused-parameter")
set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} --sysroot=${SYSROOT}")

set(CMAKE_C_FLAGS_DEBUG   "-O0 -g -DDEBUG")
set(CMAKE_C_FLAGS_RELEASE "-O2 -DNDEBUG")

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE "Release" CACHE STRING "Build type" FORCE)
endif()

# --------------------------------------------------------------------------
# Include directories
# --------------------------------------------------------------------------
include_directories(
    ${RKMPI_INC_DIR}
    ${RKMPI_INC_DIR}/rockit
    ${RKMPI_INC_DIR}/rkaiq
)

# --------------------------------------------------------------------------
# Library directories
# --------------------------------------------------------------------------
link_directories(
    ${RKMPI_LIB_DIR}
)

# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------
set(SOURCES
    src/camera_display.c
)

# --------------------------------------------------------------------------
# Executable
# --------------------------------------------------------------------------
add_executable(${PROJECT_NAME} ${SOURCES})

target_link_libraries(${PROJECT_NAME}
    rockit          # RKMPI (VI, VPSS, VO, SYS)
    pthread         # POSIX threads
)

# --------------------------------------------------------------------------
# Install (optional -- deploy to board filesystem image)
# --------------------------------------------------------------------------
install(TARGETS ${PROJECT_NAME} DESTINATION /usr/bin)
