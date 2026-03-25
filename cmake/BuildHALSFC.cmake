# BuildHALSFC.cmake - CMake module for building HAL/S-FC compiler
#

# Find GNU Make for building XCOM-I generated Makefiles
# This is separate from CMAKE_MAKE_PROGRAM because XCOM-I assumes gmake
if(WIN32)
    # Look for make in common locations
    find_program(GNU_MAKE_PROGRAM
        NAMES make mingw32-make gmake
        PATHS
            "C:/Program Files/Git/usr/bin"
            "C:/msys64/usr/bin"
            "C:/cygwin64/bin"
            "$ENV{ProgramFiles}/Git/usr/bin"
            "$ENV{ChocolateyInstall}/bin"
        DOC "GNU Make for building XCOM-I generated code"
    )
    
    if(NOT GNU_MAKE_PROGRAM)
        message(WARNING "GNU Make not found. HAL/S-FC compiler build may fail.\n"
                        "Install Git for Windows (includes make) or run:\n"
                        "  choco install make")
    else()
        message(STATUS "Found GNU Make: ${GNU_MAKE_PROGRAM}")
    endif()
    
    # Find gcc for building XCOM-I on windows
    find_program(GNU_C_COMPILER
        NAMES gcc
        PATHS
            "C:/msys64/mingw64/bin"
            "C:/msys64/ucrt64/bin"
            "C:/mingw64/bin"
            "C:/cygwin64/bin"
            "$ENV{ProgramFiles}/Git/usr/bin"
        DOC "GCC compiler for building XCOM-I generated code"
    )
    
    if(NOT GNU_C_COMPILER)
        message(WARNING "gcc requires for  HAL/S-FC build.\n")
    else()
        message(STATUS "Found GCC: ${GNU_C_COMPILER}")
    endif()
else() # Unix
    find_program(GNU_MAKE_PROGRAM NAMES make gmake)
    set(GNU_C_COMPILER "cc")
endif()

if(NOT GNU_MAKE_PROGRAM)
    set(GNU_MAKE_PROGRAM "${CMAKE_MAKE_PROGRAM}")
endif()

if(NOT GNU_C_COMPILER)
    set(GNU_C_COMPILER "${CMAKE_C_COMPILER}")
endif()

# Build a single HAL/S-FC compiler pass
# Arguments:
#   PASS_NAME   - Name of the pass (e.g., PASS1, FLO, OPT)
#   SRC_FOLDER  - Source folder relative to HALSFC_SRC_DIR (e.g., PASS1.PROCS)
#   IDENTIFIER  - 10-char identifier for XCOM-I (e.g., "REL32V0   ")
#   CONDITIONS  - List of condition flags for XCOM-I (e.g., "P;V" or "B;V")
#   OUTPUT_NAME - Output name for XCOM-I (e.g., PASS1 or PASS1B)
function(build_halsfc_pass PASS_NAME SRC_FOLDER IDENTIFIER CONDITIONS OUTPUT_NAME)
    set(TARGET_NAME "HALSFC-${PASS_NAME}")
    set(PASS_SRC_DIR "${HALSFC_SRC_DIR}/${SRC_FOLDER}")
    set(PASS_BUILD_DIR "${CMAKE_BINARY_DIR}/halsfc/${OUTPUT_NAME}.build")
    set(PASS_OUTPUT "${CMAKE_BINARY_DIR}/halsfc/HALSFC-${OUTPUT_NAME}${EXE_SUFFIX}")
    
    set(COND_ARGS "")
    foreach(cond ${CONDITIONS})
        list(APPEND COND_ARGS "--cond=${cond}")
    endforeach()
    
    set(XCOM_I_SCRIPT "${XCOM_I_DIR}/XCOM-I.py")

    set(XCOM_STAMP "${PASS_BUILD_DIR}/.xcom-stamp")

    file(MAKE_DIRECTORY "${PASS_BUILD_DIR}")

    file(GLOB XPL_SOURCES "${PASS_SRC_DIR}/*.xpl")

    set(XCOM_CMD "${Python3_EXECUTABLE}" "${XCOM_I_SCRIPT}")
    set(PASS_BINARY "HALSFC-${OUTPUT_NAME}${EXE_SUFFIX}")

    # Step 1: Run XCOM-I to translate XPL to C (output goes to build tree)
    add_custom_command(
        OUTPUT "${XCOM_STAMP}"
        BYPRODUCTS "${PASS_BUILD_DIR}"
        COMMAND ${CMAKE_COMMAND} -E env PYTHONUTF8=1
                ${XCOM_CMD}
                --identifier=${IDENTIFIER}
                ${COND_ARGS}
                --build-dir=${PASS_BUILD_DIR}
                "##DRIVER.xpl"
        COMMAND ${CMAKE_COMMAND} -E touch "${XCOM_STAMP}"
        WORKING_DIRECTORY "${PASS_SRC_DIR}"
        DEPENDS ${XPL_SOURCES}
        COMMENT "[XCOM-I] Translating ${PASS_NAME} XPL to C..."
        VERBATIM
    )

    # Step 2: Compile the generated C code.
    # The inner Makefile normally does "mv $@ .." — we override MV=true to
    # keep the binary in the build dir (prevents perpetual recompilation).
    add_custom_command(
        OUTPUT "${PASS_OUTPUT}"
        COMMAND ${GNU_MAKE_PROGRAM} "CC=${GNU_C_COMPILER}" "TARGET=${PASS_BINARY}" "MV=true"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
                "${PASS_BUILD_DIR}/${PASS_BINARY}" "${PASS_OUTPUT}"
        WORKING_DIRECTORY "${PASS_BUILD_DIR}"
        DEPENDS "${XCOM_STAMP}"
        COMMENT "[CC] Compiling ${PASS_NAME}..."
        JOB_SERVER_AWARE TRUE
        VERBATIM
    )
    
    add_custom_target(${TARGET_NAME}
        DEPENDS "${PASS_OUTPUT}"
        COMMENT "Building ${TARGET_NAME}"
    )
    
    install(
        PROGRAMS "${PASS_OUTPUT}"
        DESTINATION "${SDL_INSTALL_BINDIR}"
        COMPONENT halsfc
    )
endfunction()

# Install HAL/S-FC support files (ERRORLIB, TEMPLIB, etc.)
function(install_halsfc_support)
    # ERRORLIB
    if(EXISTS "${HALSFC_SRC_DIR}/ERRORLIB")
        install(
            DIRECTORY "${HALSFC_SRC_DIR}/ERRORLIB"
            DESTINATION "${SDL_INSTALL_LIBDIR}"
            COMPONENT halsfc
        )
    endif()
    
    # ACCESS template library
    install(
        DIRECTORY "${HALSFC_SRC_DIR}/ACCESS"
        DESTINATION "${SDL_INSTALL_LIBDIR}"
        OPTIONAL
        COMPONENT halsfc
    )
    
    # RUNMAC (macro library for assembler)
    install(
        DIRECTORY "${HALSFC_SRC_DIR}/RUNMAC"
        DESTINATION "${SDL_INSTALL_LIBDIR}"
        OPTIONAL
        COMPONENT halsfc
    )
endfunction()
