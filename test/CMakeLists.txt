#
# Tests run compiled .fcm programs through gpc-batch and compare
# output against baselines.
#
# `make test` runs tests.

# Overrides
hal_test(NAME 021-SIMPLE    MAX_STEPS 10000)
hal_test(NAME 029-DATATYPES MAX_STEPS 30000)
hal_test(NAME 037-ROOTS     MAX_STEPS 30000)
hal_test(NAME 047-ROWS      MAX_STEPS 30000)

#  need ~300K steps
hal_test(NAME bench06 MAX_STEPS 500000)
hal_test(NAME bench07 MAX_STEPS 500000)
hal_test(NAME bench08 MAX_STEPS 500000)
hal_test(NAME bench09 MAX_STEPS 500000)

# --external-syms linker tests: single-module relocation must match full link
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
    )
endforeach()

discover_hal_tests(
    MAX_STEPS 100000
    EXCLUDE
        # Already handled:
        "021-SIMPLE"
        "029-DATATYPES"
        "037-ROOTS"
        "047-ROWS"
        "bench06"
        "bench07"
        "bench08"
        "bench09"
)
