# BuildHALProgram.cmake — Compile and link HAL/S programs
#
# Provides:
#   build_hal_templates(SRC_DIR OUT_DIR TARGET_NAME PARM SOURCES...)
#   build_hal_programs(SRC_DIR OUT_DIR TARGET_NAME PARM
#                      [EXCLUDE ...] [TEMPLATES_TARGET ...])

set(HALSC_WRAPPER "${CMAKE_BINARY_DIR}/bin/halsc")
set(FCM_BUILD_DIR "${CMAKE_BINARY_DIR}/fcm")
set(HAL_TEMPLIB_DIR "${CMAKE_BINARY_DIR}/TEMPLIB")
set(HAL_COMPOOL_DIR "${CMAKE_BINARY_DIR}/COMPOOL")

# Pass DEBUG=1 on the make command line to enable linker debug output
if(DEFINED DEBUG)
    set(LNK101_EXTRA_ARGS "--debug")
else()
    set(LNK101_EXTRA_ARGS "")
endif()
file(MAKE_DIRECTORY "${FCM_BUILD_DIR}")
file(MAKE_DIRECTORY "${HAL_TEMPLIB_DIR}")
file(MAKE_DIRECTORY "${HAL_COMPOOL_DIR}")

# Build template-generating HAL/S programs into the shared TEMPLIB.
# These must be compiled before programs that use D INCLUDE TEMPLATE.
function(build_hal_templates SRC_DIR OUT_DIR TARGET_NAME PARM)
    set(_sources ${ARGN})

    file(MAKE_DIRECTORY "${OUT_DIR}")

    add_custom_target(${TARGET_NAME})
    add_dependencies(${TARGET_NAME} halsfc)

    foreach(_name IN LISTS _sources)
        set(_hal "${SRC_DIR}/${_name}.hal")
        set(_obj "${OUT_DIR}/${_name}.obj")
        set(_stamp "${OUT_DIR}/${_name}.template-stamp")
        set(_target "${TARGET_NAME}.${_name}")

        add_custom_command(
            OUTPUT "${_stamp}"
            COMMAND "${HALSC_WRAPPER}"
                "--parm=${PARM}"
                "--templib=${HAL_TEMPLIB_DIR}"
                -o "${_obj}"
                "${_hal}"
            COMMAND ${CMAKE_COMMAND} -E copy "${_obj}" "${HAL_COMPOOL_DIR}/"
            COMMAND ${CMAKE_COMMAND} -E touch "${_stamp}"
            DEPENDS "${_hal}"
            WORKING_DIRECTORY "${OUT_DIR}"
            COMMENT "Building template ${_name}"
        )
        add_custom_target(${_target} DEPENDS "${_stamp}")
        add_dependencies(${_target} halsfc)
        add_dependencies(${TARGET_NAME} ${_target})
    endforeach()
endfunction()

# build a single HAL/S program (compile + link)
function(_build_one_hal_program)
    cmake_parse_arguments(HP "" "NAME;HAL;OUT_DIR;PARM;TARGET" "" ${ARGN})

    set(_obj "${HP_OUT_DIR}/${HP_NAME}.obj")
    set(_rpt "${HP_OUT_DIR}/${HP_NAME}.pass2.rpt")
    set(_fcm "${FCM_BUILD_DIR}/${HP_NAME}.fcm")
    set(_sym "${FCM_BUILD_DIR}/${HP_NAME}.sym.json")
    set(_ext "${FCM_BUILD_DIR}/${HP_NAME}.ext.json")
    set(_lnk "${FCM_BUILD_DIR}/${HP_NAME}.lnk")
    set(_zcon "${CMAKE_BINARY_DIR}/runtime/ZCON")
    set(_run  "${CMAKE_BINARY_DIR}/runtime/RUN")

    # Compile
    add_custom_command(
        OUTPUT "${_obj}"
        COMMAND "${HALSC_WRAPPER}"
            "--parm=${HP_PARM}"
            "--templib=${HAL_TEMPLIB_DIR}"
            "--pass2-rpt=${_rpt}"
            -o "${_obj}"
            "${HP_HAL}"
        DEPENDS "${HP_HAL}"
        WORKING_DIRECTORY "${HP_OUT_DIR}"
        COMMENT "Compiling ${HP_NAME}.hal"
    )

    # Link
    add_custom_command(
        OUTPUT "${_fcm}"
        COMMAND ${CMAKE_COMMAND} -E env PYTHONUTF8=1
            "${SDL_VENV_PYTHON}" -m lnk101
            -o "${_fcm}"
            --json-symbols "${_sym}"
            --save-external-syms "${_ext}"
            --save-config "${_lnk}"
            -L "${_zcon}"
            -L "${_run}"
            -L "${HAL_COMPOOL_DIR}"
            ${LNK101_EXTRA_ARGS}
            "${_obj}"
        DEPENDS "${_obj}"
        WORKING_DIRECTORY "${FCM_BUILD_DIR}"
        COMMENT "Linking ${HP_NAME}"
    )

    # Per-program target (independent of other programs)
    add_custom_target(${HP_TARGET} DEPENDS "${_fcm}")
    add_dependencies(${HP_TARGET} halsfc runtime lnk101)
endfunction()

# Build all HAL/S programs in a source directory.
# Creates an umbrella target plus per-program subtargets.
function(build_hal_programs SRC_DIR OUT_DIR TARGET_NAME PARM)
    cmake_parse_arguments(_BHP "" "TEMPLATES_TARGET" "EXCLUDE" ${ARGN})

    if(NOT EXISTS "${SRC_DIR}")
        message(WARNING "HAL/S source directory not found: ${SRC_DIR}")
        return()
    endif()

    file(MAKE_DIRECTORY "${OUT_DIR}")

    file(GLOB _hal_files "${SRC_DIR}/*.hal")
    list(SORT _hal_files COMPARE NATURAL)
    list(LENGTH _hal_files _count)
    message(STATUS "Found ${_count} HAL/S programs for ${TARGET_NAME}")

    add_custom_target(${TARGET_NAME} ALL)

    foreach(_hal IN LISTS _hal_files)
        get_filename_component(_name "${_hal}" NAME_WE)

        # Skip excluded programs
        list(FIND _BHP_EXCLUDE "${_name}" _excl_idx)
        if(NOT _excl_idx EQUAL -1)
            continue()
        endif()

        set(_target "${TARGET_NAME}.${_name}")

        _build_one_hal_program(
            NAME "${_name}"
            HAL "${_hal}"
            OUT_DIR "${OUT_DIR}"
            PARM "${PARM}"
            TARGET "${_target}"
        )

        if(_BHP_TEMPLATES_TARGET)
            add_dependencies(${_target} ${_BHP_TEMPLATES_TARGET})
        endif()
        add_dependencies(${TARGET_NAME} ${_target})
    endforeach()

    # Install
    install(CODE "
        file(GLOB _fcms \"${FCM_BUILD_DIR}/*.fcm\")         # linked binary
        file(GLOB _syms \"${FCM_BUILD_DIR}/*.sym.json\")    # debug symbols
        file(GLOB _lnks \"${FCM_BUILD_DIR}/*.lnk\")         # linker config (w/actual locations)
        file(INSTALL \${_fcms} \${_syms} \${_lnks}
             DESTINATION \"\${CMAKE_INSTALL_PREFIX}/fcm\")
    " COMPONENT ${TARGET_NAME})
endfunction()
