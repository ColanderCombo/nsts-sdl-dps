# =====================================================================
# asm101 assembler tests.
#
# Pulled in by ../CMakeLists.txt via add_subdirectory(asm101).  This
# directory also holds the integration fixtures the feature test
# assembles (feat_*.asm, maclib_*/, proctest_main.asm, ...).
#
# The driver scripts themselves live one level up in test/; only their
# CTest registration lives here.  SDL_VENV_PYTHON and HAL_TEST_SRC_DIR
# are inherited from the top-level CMakeLists.
#
# Run them with:  make test-asm101          (all of them)
#                 make test-asm101-features (one of them)
#             or:  ctest -L asm101           (from the build dir)
# =====================================================================

# The test scripts sit in test/ (the parent of this fixture directory).
set(_asm_script_dir "${CMAKE_CURRENT_SOURCE_DIR}/..")

# End-to-end smoke test for upstream ext/virtualagc/ASM101S: assembles a
# tiny ZCONASM fixture and byte-compares the .obj to a checked-in golden.
# Catches breakage in the upstream submodule (parser, codegen, object
# writer) without depending on our src/asm101 fork.
add_test(NAME asm101_upstream
    COMMAND "${SDL_VENV_PYTHON}"
        "${_asm_script_dir}/test_asm101_upstream.py"
)
set_tests_properties(asm101_upstream PROPERTIES
    LABELS "submodule;asm101"
    TIMEOUT 30
)

# Feature / regression tests for the src/asm101 fork: unit checks of the
# parser/evaluator plus integration assembly of small fixtures under
# test/asm101/.  Locks down behaviors fixed while bringing real flight
# software (e.g. BILDNEW5) through the assembler.
add_test(NAME asm101_features
    COMMAND "${SDL_VENV_PYTHON}"
        "${_asm_script_dir}/test_asm101_features.py"
)
set_tests_properties(asm101_features PROPERTIES
    LABELS "asm101"
    TIMEOUT 180
)

# Byte-golden regression: assembles every test/asm101/*.asm fixture with
# default flags and byte-compares each producer against a checked-in golden in
# test/asm101/golden/.  The broad "did the emitted bytes move" net behind the
# property checks in test_asm101_features.  Regenerate after an intentional
# output change with:  python test/test_asm101_golden.py --regen
add_test(NAME asm101_golden
    COMMAND "${SDL_VENV_PYTHON}"
        "${_asm_script_dir}/test_asm101_golden.py"
)
set_tests_properties(asm101_golden PROPERTIES
    LABELS "asm101"
    TIMEOUT 120
)

# Instruction-definition sync guard: runs the ext/sim harness to extract the GPC
# simulator's instruction definitions, then checks they match the checked-in
# src/asm101/instr_defs.json (freshness) and that the IOP descriptors still
# agree with the listing-correct reference in test/iop_listing_truth.py (catches
# the coffee drifting from flight-listing truth).  SKIPs cleanly if Node/esbuild
# or the ext/sim submodule are unavailable.
add_test(NAME asm101_instr_defs
    COMMAND "${SDL_VENV_PYTHON}"
        "${_asm_script_dir}/test_asm101_instr_defs.py"
)
set_tests_properties(asm101_instr_defs PROPERTIES
    LABELS "asm101"
    TIMEOUT 120
)

# Compiled-archive differential: asserts the descriptor-driven CPU encoder
# reproduces real HAL/S-FC object code taken from the BFS COMPILED listings (RS
# extended/indexed long forms + RR).  Independent ground truth -- not asm101's
# own output -- so it catches descriptor/sentinel-scheme errors the byte-golden
# self-check cannot.
add_test(NAME asm101_compiled_vectors
    COMMAND "${SDL_VENV_PYTHON}"
        "${_asm_script_dir}/test_asm101_compiled_vectors.py"
)
set_tests_properties(asm101_compiled_vectors PROPERTIES
    LABELS "asm101"
    TIMEOUT 30
)

# IOP (MSC/BCE) instruction-encoder test: a descriptor self-test (integrity +
# encode/decode round-trip) plus, when the BFS flight listings are present,
# byte-level validation of every MSC/BCE instruction line's object code against
# the asm101.iop_instr encoder (now sourced from instr_defs.json).  Self-test
# always runs; the listing check skips cleanly if the (out-of-tree) listings are
# unavailable.
add_test(NAME asm101_iop
    COMMAND "${SDL_VENV_PYTHON}"
        "${_asm_script_dir}/test_asm101_iop.py"
)
set_tests_properties(asm101_iop PROPERTIES
    LABELS "asm101"
    TIMEOUT 60
)

# ---------------------------------------------------------------------
# Convenience run targets.  Each builds the assembler (so the venv has
# the fork installed) then runs the relevant tests via ctest.  The
# top-level build target is named `asm101`, so these are `test-asm101*`.
# ---------------------------------------------------------------------
add_custom_target(test-asm101
    COMMAND "${CMAKE_CTEST_COMMAND}" -L asm101 --output-on-failure
    DEPENDS asm101
    WORKING_DIRECTORY "${CMAKE_BINARY_DIR}"
    COMMENT "Running all asm101 tests"
    USES_TERMINAL
)

# One target per test, for tight iteration on a single suite.  The ctest -R
# pattern is left unanchored on purpose: a trailing '$' is swallowed by Make's
# variable expansion in the generated recipe, and each test name is already a
# unique substring (no asm101_* name is a substring of another).
foreach(_asm_test asm101_upstream asm101_features asm101_iop asm101_golden asm101_instr_defs asm101_compiled_vectors)
    # test-asm101-features -> a hyphenated alias is friendlier to type.
    string(REPLACE "asm101_" "test-asm101-" _asm_tgt "${_asm_test}")
    add_custom_target(${_asm_tgt}
        COMMAND "${CMAKE_CTEST_COMMAND}" -R "${_asm_test}" --output-on-failure
        DEPENDS asm101
        WORKING_DIRECTORY "${CMAKE_BINARY_DIR}"
        COMMENT "Running ${_asm_test}"
        USES_TERMINAL
    )
endforeach()
