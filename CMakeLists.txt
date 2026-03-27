cmake_minimum_required(VERSION 3.28)

if(CMAKE_INSTALL_PREFIX_INITIALIZED_TO_DEFAULT OR NOT DEFINED CMAKE_INSTALL_PREFIX)
    set(CMAKE_INSTALL_PREFIX "${CMAKE_CURRENT_SOURCE_DIR}/inst" CACHE PATH "Installation prefix" FORCE)
endif()

project(SDL
        VERSION 1.0.0
        DESCRIPTION "AP-101 toolchain"
        LANGUAGES C)

set(SDL_INSTALL_BINDIR "${CMAKE_INSTALL_PREFIX}/bin" CACHE PATH "Binary install dir")
set(SDL_INSTALL_LIBDIR "${CMAKE_INSTALL_PREFIX}/lib" CACHE PATH "Library install dir")

# Paths to toolchain source (via symlinks into ext/virtualagc)
set(XCOM_I_DIR "${CMAKE_SOURCE_DIR}/src/XCOM-I" CACHE PATH "Path to XCOM-I")
set(HALSFC_SRC_DIR "${CMAKE_SOURCE_DIR}/code/PASS.REL32V0" CACHE PATH "Path to HAL/S-FC source")
set(ASM101S_DIR "${CMAKE_SOURCE_DIR}/src/ASM101S" CACHE PATH "Path to ASM101S")
set(LNK101S_DIR "${CMAKE_SOURCE_DIR}/src/LNK101")

list(APPEND CMAKE_MODULE_PATH ${CMAKE_SOURCE_DIR}/cmake)
include(BuildHALSFC)
include(BuildRuntime)
include(BuildHALProgram)

find_package(Python3 COMPONENTS Interpreter REQUIRED)

message(STATUS "C Compiler: ${CMAKE_C_COMPILER}")

if(WIN32)
    set(EXE_SUFFIX ".exe")
else()
    set(EXE_SUFFIX "")
endif()

#-----------------------------------------------------------------------------
# Python venv via uv workspace (installs ASM101S + LNK101S)
#-----------------------------------------------------------------------------
set(SDL_VENV_DIR "${CMAKE_BINARY_DIR}/venv")
set(SDL_VENV_PYTHON "${SDL_VENV_DIR}/bin/python")

include(Uv)
uv_initialize(
    PYTHON_VERSION 3.13
    MANAGED_PYPROJECT_FILE "${CMAKE_BINARY_DIR}/pyproject.toml"
    WORKSPACE_PACKAGE_NAME "sdl"
    WORKSPACE_VENV "${SDL_VENV_DIR}"
)
uv_add_pyproject("src/ASM101S/pyproject.toml")
uv_add_pyproject("src/LNK101/pyproject.toml")

#-----------------------------------------------------------------------------
# asm101s / lnk101
#-----------------------------------------------------------------------------
add_custom_target(asm101s ALL COMMENT "ASM101S assembler")
add_dependencies(asm101s uv_sync)

add_custom_target(lnk101 ALL COMMENT "LNK101S linker")
add_dependencies(lnk101 uv_sync)

#-----------------------------------------------------------------------------
# HAL/S-FC Compiler
#-----------------------------------------------------------------------------
# PFS passes
build_halsfc_pass(PASS1 "PASS1.PROCS" "REL32V0   " "P;V" "PASS1")
build_halsfc_pass(FLO   "FLO.PROCS"   "REL32V0   " "P;V" "FLO")
build_halsfc_pass(OPT   "OPT.PROCS"   "REL32V0   " "P;V" "OPT")
build_halsfc_pass(AUXP  "AUX_PROCS"   "REL32V0   " "P;V" "AUXP")
build_halsfc_pass(PASS2 "PASS2.PROCS" "REL32V0   " "P;V" "PASS2")
build_halsfc_pass(PASS3 "PASS3.PROCS" "REL32V0   " "P;V" "PASS3")
build_halsfc_pass(PASS4 "PASS4.PROCS" "REL32V0   " "P;V" "PASS4")

# BFS passes (shares FLO, AUXP, PASS4 with PFS)
build_halsfc_pass(PASS1B "PASS1.PROCS" "BFC-17.0  " "B;V" "PASS1B")
build_halsfc_pass(OPTB   "OPT.PROCS"   "BFC-17.0  " "B;V" "OPTB")
build_halsfc_pass(PASS2B "PASS2.PROCS" "BFC-17.0  " "B;V" "PASS2B")
build_halsfc_pass(PASS3B "PASS3.PROCS" "BFC-17.0  " "B;V" "PASS3B")

add_custom_target(halsfc ALL COMMENT "HAL/S-FC compiler")
add_dependencies(halsfc
    HALSFC-PASS1 HALSFC-FLO HALSFC-OPT HALSFC-AUXP
    HALSFC-PASS2 HALSFC-PASS3 HALSFC-PASS4
    HALSFC-PASS1B HALSFC-OPTB HALSFC-PASS2B HALSFC-PASS3B
)

#-----------------------------------------------------------------------------
# Runtime Library (RUNASM+ZCONASM)
#-----------------------------------------------------------------------------
build_runtime_library()
add_dependencies(runtime asm101s)

#-----------------------------------------------------------------------------
# HAL/S sample programs
#-----------------------------------------------------------------------------
set(PROGHAL_SRC_DIR "${CMAKE_SOURCE_DIR}/test/halTests/progHal")
set(BENCH_SRC_DIR "${CMAKE_SOURCE_DIR}/test/halTests/BENCH")
set(HAL_SAMPLE_PARM "NOTABLES,LIST,CARDTYPE=YCZM")

# Template-generating compools — must be built first so that programs
# using "D INCLUDE TEMPLATE CPLxxx" can find them in the TEMPLIB.
build_hal_templates("${PROGHAL_SRC_DIR}" "${CMAKE_BINARY_DIR}/progHal"
    "templates" "TEMPLATE,NOTABLES,LISTING2"
    189-IMU_DATA
    264-TQE
    269-PROCESS_CONTROL
)

set(HAL_EXCLUDE_PROGRAMS
    167-ASSORTEDIO # requires NONHAL IO(1); we don't have right now.
    176-P
    224-GNC_POOL
)
build_hal_programs("${PROGHAL_SRC_DIR}" "${CMAKE_BINARY_DIR}/progHal"
                   "proghal" "${HAL_SAMPLE_PARM}"
                   TEMPLATES_TARGET templates
                   EXCLUDE ${HAL_EXCLUDE_PROGRAMS})
build_hal_programs("${BENCH_SRC_DIR}" "${CMAKE_BINARY_DIR}/bench"
                   "bench" "${HAL_SAMPLE_PARM}")

#-----------------------------------------------------------------------------
# GPC simulator
#-----------------------------------------------------------------------------
set(SIM_DIR "${CMAKE_SOURCE_DIR}/ext/sim" CACHE PATH "Path to GPC simulator")
find_program(NODE_EXECUTABLE node REQUIRED)

file(GLOB_RECURSE GPC_SIM_SOURCES
    "${SIM_DIR}/gpc/*.coffee"
    "${SIM_DIR}/gpc/*.ts"
    "${SIM_DIR}/simRunner/main/*.civet"
    "${SIM_DIR}/simRunner/renderer/*.civet"
    "${SIM_DIR}/simRunner/renderer/*.tsx"
    "${SIM_DIR}/simRunner/renderer/*.ts"
    "${SIM_DIR}/simRunner/renderer/*.css"
    "${SIM_DIR}/simRunner/renderer/*.html"
)
list(APPEND GPC_SIM_SOURCES
    "${SIM_DIR}/esbuild/esbuild.batch.config.js"
    "${SIM_DIR}/esbuild/esbuild.dbg.config.js"
    "${SIM_DIR}/electron-esbuild.config.yaml"
    "${SIM_DIR}/package.json"
)

set(GPC_SIM_STAMP "${CMAKE_BINARY_DIR}/gpc-sim.stamp")
set(GPC_NPM_STAMP "${CMAKE_BINARY_DIR}/gpc-npm.stamp")
add_custom_command(
    OUTPUT "${GPC_NPM_STAMP}"
    COMMAND npm install
    COMMAND ${CMAKE_COMMAND} -E touch "${GPC_NPM_STAMP}"
    WORKING_DIRECTORY "${SIM_DIR}"
    DEPENDS "${SIM_DIR}/package.json"
    COMMENT "Installing GPC simulator npm dependencies"
    VERBATIM
)
add_custom_command(
    OUTPUT "${GPC_SIM_STAMP}"
    COMMAND "${SIM_DIR}/node_modules/.bin/electron-esbuild" build
    COMMAND "${NODE_EXECUTABLE}" "${SIM_DIR}/esbuild/esbuild.batch.config.js"
    COMMAND "${NODE_EXECUTABLE}" "${SIM_DIR}/esbuild/esbuild.fcmdump.config.js"
    COMMAND "${NODE_EXECUTABLE}" "${SIM_DIR}/esbuild/esbuild.dbg.config.js"
    COMMAND ${CMAKE_COMMAND} -E touch "${GPC_SIM_STAMP}"
    WORKING_DIRECTORY "${SIM_DIR}"
    DEPENDS "${GPC_NPM_STAMP}" ${GPC_SIM_SOURCES}
    COMMENT "Building GPC simulator (batch + debugger)"
    VERBATIM
)
add_custom_target(gpc-sim ALL DEPENDS "${GPC_SIM_STAMP}")

#-----------------------------------------------------------------------------
# Clean target — remove build artifacts but preserve venv and CMake state
#-----------------------------------------------------------------------------
# Make 'make clean' remove our build artifact directories.
# (Generates CMakeFiles/cmake_directory_clean.cmake with REMOVE_RECURSE.)
set_property(DIRECTORY APPEND PROPERTY ADDITIONAL_CLEAN_FILES
    "${CMAKE_BINARY_DIR}/halsfc"
    "${CMAKE_BINARY_DIR}/fcm"
    "${CMAKE_BINARY_DIR}/progHal"
    "${CMAKE_BINARY_DIR}/bench"
    "${CMAKE_BINARY_DIR}/runtime"
    "${CMAKE_BINARY_DIR}/TEMPLIB"
    "${CMAKE_BINARY_DIR}/COMPOOL"
    "${CMAKE_BINARY_DIR}/gpc-sim.stamp"
    "${CMAKE_BINARY_DIR}/gpc-npm.stamp"
)

#-----------------------------------------------------------------------------
# Install wrapper scripts and support files
#-----------------------------------------------------------------------------
include(InstallWrappers)

#-----------------------------------------------------------------------------
# Tests (ctest)
#-----------------------------------------------------------------------------
set(GPC_BATCH_WRAPPER "${CMAKE_BINARY_DIR}/bin/gpc-batch")

set(HAL_TEST_FCM_DIR "${CMAKE_BINARY_DIR}/fcm")
set(HAL_TEST_SRC_DIR "${CMAKE_SOURCE_DIR}/test")
set(HAL_TEST_BASELINE_DIR "${CMAKE_SOURCE_DIR}/test/baselines")

include(TestHalProgram)
enable_testing()
add_subdirectory(test)
