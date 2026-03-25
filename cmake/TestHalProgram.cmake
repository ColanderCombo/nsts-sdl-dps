# TestHalProgram.cmake — HAL/S program test infrastructure
#
#   FCM file:   ${HAL_TEST_FCM_DIR}/<name>.fcm
#   Baseline:   ${HAL_TEST_BASELINE_DIR}/<name>.expected.out6  (if exists → comparison)
#   Input:      ${HAL_TEST_SRC_DIR}/<name>.in5                 (if exists → --infile5)
#   Output:     ${CMAKE_CURRENT_BINARY_DIR}/<name>.actual.out6

cmake_minimum_required(VERSION 4.0)

# hal_test(NAME <name> [MAX_STEPS <n>])
#
# If a baseline exists, a second fixture test compares output via diff -u.
# If an .in5 file exists, it's passed as simulator input.
function(hal_test)
    cmake_parse_arguments(HT "" "NAME;MAX_STEPS" "" ${ARGN})

    if(NOT HT_NAME)
        message(FATAL_ERROR "hal_test requires NAME")
    endif()
    if(NOT HT_MAX_STEPS)
        set(HT_MAX_STEPS 50000)
    endif()

    set(_fcm "${HAL_TEST_FCM_DIR}/${HT_NAME}.fcm")
    set(_actual "${CMAKE_CURRENT_BINARY_DIR}/${HT_NAME}.actual.out6")
    set(_baseline "${HAL_TEST_BASELINE_DIR}/${HT_NAME}.expected.out6")
    set(_infile5 "${HAL_TEST_SRC_DIR}/${HT_NAME}.in5")

    # Build the gpc-batch command
    set(_cmd
        "${GPC_BATCH_WRAPPER}"
        "${_fcm}"
        "--no-trace"
        "--max-steps" "${HT_MAX_STEPS}"
        "--outfile6=${_actual}"
    )
    if(EXISTS "${_infile5}")
        list(APPEND _cmd "--infile5=${_infile5}")
    endif()

    set(_labels "hal")

    if(EXISTS "${_baseline}")
        # Run test produces output, comparison test checks it
        add_test(NAME "${HT_NAME}" COMMAND ${_cmd})
        set_tests_properties("${HT_NAME}" PROPERTIES
            LABELS "${_labels}"
            TIMEOUT 120
            FIXTURES_SETUP "${HT_NAME}_OUTPUT"
        )

        add_test(NAME "${HT_NAME}.compare"
            COMMAND diff -u "${_baseline}" "${_actual}")
        set_tests_properties("${HT_NAME}.compare" PROPERTIES
            LABELS "${_labels}"
            TIMEOUT 10
            FIXTURES_REQUIRED "${HT_NAME}_OUTPUT"
        )
    else()
        # Run-only test (crash/hang detection)
        list(APPEND _labels "run-only")
        add_test(NAME "${HT_NAME}" COMMAND ${_cmd})
        set_tests_properties("${HT_NAME}" PROPERTIES
            LABELS "${_labels}"
            TIMEOUT 120
        )
    endif()
endfunction()


# discover_hal_tests([MAX_STEPS <n>] [EXCLUDE <name> ...])
#
# Scan HAL/S source directories for programs and register tests for each.
#
# Programs needing input (per .status file) with no .in5 file are disabled.
# All other programs get registered — with baseline comparison if a baseline
# exists, or as run-only otherwise.
function(discover_hal_tests)
    cmake_parse_arguments(DISC "" "MAX_STEPS" "EXCLUDE;SRC_DIRS" ${ARGN})

    if(NOT DISC_MAX_STEPS)
        set(DISC_MAX_STEPS 100000)
    endif()

    if(NOT DISC_SRC_DIRS)
        set(DISC_SRC_DIRS "${PROGHAL_SRC_DIR}" "${BENCH_SRC_DIR}")
    endif()

    set(_all_names "")
    foreach(_src_dir ${DISC_SRC_DIRS})
        if(EXISTS "${_src_dir}")
            file(GLOB _hal_files "${_src_dir}/*.hal")
            foreach(_hal IN LISTS _hal_files)
                get_filename_component(_name "${_hal}" NAME_WE)
                list(APPEND _all_names "${_name}")
            endforeach()
        endif()
    endforeach()
    list(REMOVE_DUPLICATES _all_names)
    list(SORT _all_names COMPARE NATURAL)

    foreach(_name IN LISTS _all_names)
        list(FIND DISC_EXCLUDE "${_name}" _idx)
        if(NOT _idx EQUAL -1)
            continue()
        endif()

        if(TEST "${_name}")
            continue()
        endif()

        # Skip programs that need input we don't have
        set(_status_file "${HAL_TEST_BASELINE_DIR}/${_name}.status")
        set(_infile5 "${HAL_TEST_SRC_DIR}/${_name}.in5")
        if(EXISTS "${_status_file}" AND NOT EXISTS "${_infile5}")
            file(READ "${_status_file}" _status_content)
            if(_status_content MATCHES "input=no" AND _status_content MATCHES "exit=1")
                add_test(NAME "${_name}"
                    COMMAND "${CMAKE_COMMAND}" -E echo
                        "${_name}: SKIPPED — Needs READ(5) input (.in5 file)")
                set_tests_properties("${_name}" PROPERTIES
                    LABELS "hal;needs-input"
                    DISABLED TRUE
                )
                continue()
            endif()
        endif()

        hal_test(NAME "${_name}" MAX_STEPS "${DISC_MAX_STEPS}")
    endforeach()
endfunction()
