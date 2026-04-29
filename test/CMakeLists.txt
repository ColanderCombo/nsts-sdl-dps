#
# Tests run compiled .fcm programs through gpc-batch and compare
# output against baselines.
#
# gpc-batch exits 0 on clean halt, 1 on max-steps. Reaching max-steps
# is always a test failure.
#
# `make test` runs tests.

# Programs that need custom step budgets
hal_test(NAME 021-SIMPLE                  MAX_STEPS 10000)
hal_test(NAME 071-DARTBOARD_APPROXIMATION MAX_STEPS 500000)
hal_test(NAME 104-EXAMPLE_1               MAX_STEPS 500000)
hal_test(NAME bench02                     MAX_STEPS 5000000)
hal_test(NAME bench03                     MAX_STEPS 5000000)
# bench04: compiler stack layout collision — C0MATVEC's DO-FOR-TEMPORARY-IX
# counter at offset 19 overlaps MATRIX_DET temp, corrupts ITOD code:
# hal_test(NAME bench04 MAX_STEPS 50000000)
hal_test(NAME bench05 MAX_STEPS 10000000)
hal_test(NAME bench06 MAX_STEPS 500000)
hal_test(NAME bench07 MAX_STEPS 500000)
hal_test(NAME bench08 MAX_STEPS 500000)
hal_test(NAME bench09 MAX_STEPS 500000)
hal_test(NAME bench10 MAX_STEPS 1000000)
hal_test(NAME bench11 MAX_STEPS 1000000)

foreach(_prog bench02 bench08)
    add_test(NAME "external-syms.${_prog}"
        COMMAND "${SDL_VENV_PYTHON}"
            "${CMAKE_CURRENT_SOURCE_DIR}/test_external_syms.py"
            "${_prog}"
            "${CMAKE_BINARY_DIR}"
            "${SDL_VENV_PYTHON}"
    )
    set_tests_properties("external-syms.${_prog}" PROPERTIES
        LABELS "linker"
        TIMEOUT 30
        FIXTURES_REQUIRED "ENV_READY"
    )
endforeach()

discover_hal_tests(
    MAX_STEPS 100000
    EXCLUDE
        # Already handled above:
        "021-SIMPLE"
        "071-DARTBOARD_APPROXIMATION"
        "104-EXAMPLE_1"
        "bench02"
        "bench03"
        "bench04"
        "bench05"
        "bench06"
        "bench07"
        "bench08"
        "bench09"
        "bench10"
        "bench11"
        ${HAL_EXCLUDE_PROGRAMS}
)

# Unit tests (test/unit/*.hal)
discover_hal_tests(
    MAX_STEPS 500000
    SRC_DIRS "${UNIT_SRC_DIR}"
)

# Regression tests (test/regress/*.hal)
discover_hal_tests(
    MAX_STEPS 500000
    SRC_DIRS "${REGRESS_SRC_DIR}"
)

# Direct C-level unit tests of the IBM hex DP primitives in
# ext/virtualagc/XCOM-I/ibmFloat.{c,h}.  Exercises the arithmetic and
# conversion code without going through the compile/sim pipeline.
set(_ibm_float_dir "${CMAKE_SOURCE_DIR}/ext/virtualagc/XCOM-I")
add_executable(test_ibm_float
    "${CMAKE_CURRENT_SOURCE_DIR}/test_ibm_float.c"
    "${_ibm_float_dir}/ibmFloat.c"
)
target_include_directories(test_ibm_float PRIVATE "${_ibm_float_dir}")
target_link_libraries(test_ibm_float PRIVATE m)
add_test(NAME test_ibm_float COMMAND test_ibm_float)
set_tests_properties(test_ibm_float PROPERTIES LABELS "unit")
