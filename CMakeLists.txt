# ===========================================================================
# CMakeLists.txt -- AI-HUD Camera Display + NPU Inference Pipeline
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
#   scp model/yolov5n_320.rknn root@172.32.0.93:/root/model/
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
# RV1106 platform define (selects NHWC output query in RKNN API)
# --------------------------------------------------------------------------
add_definitions(-DRV1106_RV1103)

# --------------------------------------------------------------------------
# Include directories
# --------------------------------------------------------------------------
include_directories(
    ${CMAKE_SOURCE_DIR}/src       # Project headers (rknn_detect.h, postprocess.h)
    ${RKMPI_INC_DIR}
    ${RKMPI_INC_DIR}/rockit
    ${RKMPI_INC_DIR}/rkaiq
    ${RKMPI_INC_DIR}/rknn         # RKNN API headers (rknn_api.h)
)

# --------------------------------------------------------------------------
# Library directories
#
# On RV1106, the RKNN runtime (librknnmrt.so) is typically installed to
# /oem/usr/lib on the board. The sysroot should mirror this layout, or
# the library can be found under the standard /usr/lib in the sysroot.
# --------------------------------------------------------------------------
link_directories(
    ${RKMPI_LIB_DIR}
    ${SYSROOT}/oem/usr/lib         # RV1106 RKNN runtime (librknnmrt.so)
)

# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------
set(SOURCES
    src/camera_display.c
    src/rknn_detect.c
    src/postprocess.c
)

# --------------------------------------------------------------------------
# Executable
# --------------------------------------------------------------------------
add_executable(${PROJECT_NAME} ${SOURCES})

target_link_libraries(${PROJECT_NAME}
    rockit          # RKMPI (VI, VPSS, VO, SYS)
    rknnmrt         # RKNN NPU runtime (RV1106 mini runtime, NOT librknnrt.so)
    pthread         # POSIX threads
    m               # Math library (expf, logf for sigmoid/postprocess)
)

# --------------------------------------------------------------------------
# Install (optional -- deploy to board filesystem image)
# --------------------------------------------------------------------------
install(TARGETS ${PROJECT_NAME} DESTINATION /usr/bin)
