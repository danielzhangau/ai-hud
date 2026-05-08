# ===========================================================================
# CMakeLists.txt -- AI-HUD Camera Display + NPU Inference Pipeline
#
# Cross-compile for Luckfox Pico Ultra (RV1106G3) using Buildroot toolchain
#
# ---- Mode A: Lightweight build (recommended for app development) ----
#   Uses standalone toolchain + RKMPI example repo for headers/libs.
#
#   cmake .. \
#     -DCMAKE_TOOLCHAIN_FILE=../cmake/rv1106-toolchain.cmake \
#     -DTOOLCHAIN_DIR=/path/to/arm-rockchip830-linux-uclibcgnueabihf \
#     -DRKMPI_DIR=/path/to/luckfox_pico_rkmpi_example
#
# ---- Mode B: Full SDK build ----
#   Uses the full Luckfox SDK sysroot (from sdk-build workflow).
#
#   export LUCKFOX_SDK_PATH=/path/to/luckfox-pico
#   cmake .. \
#     -DCMAKE_C_COMPILER=$LUCKFOX_SDK_PATH/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf/bin/arm-rockchip830-linux-uclibcgnueabihf-gcc \
#     -DSYSROOT=$LUCKFOX_SDK_PATH/sysdrv/out/rootfs_uclibc_rv1106
#
# Deploy:
#   adb push ai-hud /root/
#   adb push models/yolov5n_320.rknn /root/model/
# ===========================================================================

cmake_minimum_required(VERSION 3.10)
project(ai-hud C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

# --------------------------------------------------------------------------
# Determine build mode: RKMPI_DIR (lightweight) vs SYSROOT (full SDK)
# --------------------------------------------------------------------------
if(DEFINED RKMPI_DIR)
    # ---- Mode A: Lightweight build using RKMPI example repo ----
    message(STATUS "Build mode: Lightweight (RKMPI_DIR)")
    message(STATUS "RKMPI_DIR = ${RKMPI_DIR}")

    set(RKMPI_INC_DIR "${RKMPI_DIR}/include")
    set(RKMPI_LIB_DIR "${RKMPI_DIR}/lib/uclibc")

elseif(DEFINED ENV{RKMPI_DIR})
    set(RKMPI_DIR "$ENV{RKMPI_DIR}")
    message(STATUS "Build mode: Lightweight (RKMPI_DIR from env)")
    message(STATUS "RKMPI_DIR = ${RKMPI_DIR}")

    set(RKMPI_INC_DIR "${RKMPI_DIR}/include")
    set(RKMPI_LIB_DIR "${RKMPI_DIR}/lib/uclibc")

else()
    # ---- Mode B: Full SDK sysroot ----
    message(STATUS "Build mode: Full SDK (SYSROOT)")

    if(NOT DEFINED SYSROOT)
        if(DEFINED ENV{LUCKFOX_SDK_PATH})
            set(SYSROOT "$ENV{LUCKFOX_SDK_PATH}/sysdrv/out/rootfs_uclibc_rv1106"
                CACHE PATH "Sysroot for cross compilation")
        else()
            set(SYSROOT "/opt/luckfox-sysroot"
                CACHE PATH "Sysroot for cross compilation")
        endif()
    endif()

    message(STATUS "SYSROOT = ${SYSROOT}")

    set(RKMPI_INC_DIR "${SYSROOT}/usr/include" CACHE PATH "RKMPI header directory")
    set(RKMPI_LIB_DIR "${SYSROOT}/usr/lib"     CACHE PATH "RKMPI library directory")

    # Full SDK mode: override sysroot in compiler flags
    set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} --sysroot=${SYSROOT}")
endif()

# --------------------------------------------------------------------------
# Compiler flags
# --------------------------------------------------------------------------
set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -Wall -Wextra -Wno-unused-parameter")
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
    ${CMAKE_SOURCE_DIR}/src       # Project headers (rknn_detect.h, postprocess.h, hud_ipc.h)
    ${RKMPI_INC_DIR}
    ${RKMPI_INC_DIR}/rockit
    ${RKMPI_INC_DIR}/rkaiq
    ${RKMPI_INC_DIR}/rknn         # RKNN API headers (rknn_api.h)
)

# --------------------------------------------------------------------------
# Library directories
#
# -rpath-link tells the linker where to resolve transitive shared library
# dependencies (e.g. librockit.so needs librockchip_mpp.so.1 and librga.so).
# --------------------------------------------------------------------------
link_directories(${RKMPI_LIB_DIR})
set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} -Wl,-rpath-link,${RKMPI_LIB_DIR}")

# For full SDK mode, also check the oem path
if(DEFINED SYSROOT)
    link_directories(${SYSROOT}/oem/usr/lib)
endif()

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
    rockit_full     # RKMPI full (VI, VPSS, VO, SYS -- rockit_tiny lacks VO)
    rockchip_mpp    # Rockchip MPP codec/output backend
    rga             # 2D graphics acceleration (used by rockit internally)
    rknnmrt         # RKNN NPU runtime (RV1106 mini runtime)
    pthread         # POSIX threads
    m               # Math library (expf, logf for sigmoid/postprocess)
)

# --------------------------------------------------------------------------
# Install (optional -- deploy to board filesystem image)
# --------------------------------------------------------------------------
install(TARGETS ${PROJECT_NAME} DESTINATION /usr/bin)
