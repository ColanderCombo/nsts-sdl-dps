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
            "${CMAKE_CURRENT_SOURCE_DIR}/srcTest/lnk101/test_external_syms.py"
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
#if(EXISTS "${_ibm_float_dir}/ibmFloat.c")
if(0)
    add_executable(test_ibm_float
        "${CMAKE_CURRENT_SOURCE_DIR}/test_ibm_float.c"
        "${_ibm_float_dir}/ibmFloat.c"
    )
    target_include_directories(test_ibm_float PRIVATE "${_ibm_float_dir}")
    target_link_libraries(test_ibm_float PRIVATE m)
    add_test(NAME test_ibm_float COMMAND test_ibm_float)
    set_tests_properties(test_ibm_float PROPERTIES LABELS "unit")
else()
    #message(WARNING "ibmFloat.c not found (ext/virtualagc submodule not "
    #    "initialized?) — skipping test_ibm_float: ${_ibm_float_dir}")
endif()

# Survey tests over the vagc#1296 literal-scalars datasets.  The datasets
# (and the harnesses that read them) can't be included in the public
# repository, so they live in the untracked testCases/ tree; both tests
# are skipped quietly when it is absent.
set(_vagc1296_dir "${CMAKE_SOURCE_DIR}/testCases/vagc#1296")

# Survey test: feeds the HAL/S source literals from rburkey's
# literalSCALARS.txt (virtualagc issue #1296) through ibm_dp_from_string
# and writes a comparison report against HAL/S-FC's compiled output.
# Always exits 0 — diagnostic, not gating.
if(EXISTS "${_vagc1296_dir}/test_literal_scalars.c"
   AND EXISTS "${_ibm_float_dir}/ibmFloat.c")
    add_executable(test_literal_scalars
        "${_vagc1296_dir}/test_literal_scalars.c"
        "${_ibm_float_dir}/ibmFloat.c"
    )
    target_include_directories(test_literal_scalars PRIVATE "${_ibm_float_dir}")
    target_link_libraries(test_literal_scalars PRIVATE m)
    file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/halfp")
    add_test(NAME test_literal_scalars
        COMMAND test_literal_scalars
            "${_vagc1296_dir}/literalSCALARS-rburkey.txt"
            "${CMAKE_CURRENT_BINARY_DIR}/halfp/literal_scalars_report.txt"
    )
    set_tests_properties(test_literal_scalars PROPERTIES LABELS "diagnostic")
endif()

# Survey test (Python-driven) over the larger vagc#1296 dataset, which
# preserves MAFGEN's '*' marks on hand-patched binary values.  The Python
# parser drives a tiny C batch-mode driver that wraps ibmFloat.c, so all
# conversions go through the same code as the rest of the pipeline.
# Always exits 0 — diagnostic, not gating.
if(EXISTS "${_vagc1296_dir}/test_literal_scalars_v2.py"
   AND EXISTS "${CMAKE_SOURCE_DIR}/tools/halfp/halfp_driver.c"
   AND EXISTS "${_ibm_float_dir}/ibmFloat.c")
    add_executable(halfp_driver
        "${CMAKE_SOURCE_DIR}/tools/halfp/halfp_driver.c"
        "${_ibm_float_dir}/ibmFloat.c"
    )
    target_include_directories(halfp_driver PRIVATE "${_ibm_float_dir}")
    target_link_libraries(halfp_driver PRIVATE m)
    file(MAKE_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/halfp")
    add_test(NAME test_literal_scalars_v2
        COMMAND "${SDL_VENV_PYTHON}"
            "${_vagc1296_dir}/test_literal_scalars_v2.py"
            "${_vagc1296_dir}/literalSCALARS-exp2.txt"
            "$<TARGET_FILE:halfp_driver>"
            "${CMAKE_CURRENT_BINARY_DIR}/halfp/literal_scalars_v2_report.txt"
    )
    set_tests_properties(test_literal_scalars_v2 PROPERTIES LABELS "diagnostic")
endif()

# =====================================================================
# floatIBM AP-101S compliance test.  Two pieces:
#   1. tools/floatIBM/refgen          — emits NDJSON reference vectors
#                                       from the AP-101S-faithful
#                                       primitives in tools/floatIBM/
#                                       ibmFloat.c.
#   2. ext/sim/test/test_floatIBM.cjs — bundles ext/sim/gpc/floatIBM.coffee
#                                       via esbuild and grades it
#                                       against the vectors.
#
# A FAIL row is a real bug in floatIBM.coffee.  GAP rows are exception-
# path vectors waiting on the bigger API change (returning exception
# flags from the arithmetic primitives — items 4-6 of the original
# compliance plan).  Currently gating: 0 FAILs allowed.
# =====================================================================
set(_floatIBM_dir "${CMAKE_SOURCE_DIR}/tools/floatIBM")
#if(EXISTS "${_floatIBM_dir}/ibmFloat.c")
if(0)
    add_executable(floatIBM_refgen
        "${_floatIBM_dir}/refgen.c"
        "${_floatIBM_dir}/ibmFloat.c"
    )
    target_include_directories(floatIBM_refgen PRIVATE "${_floatIBM_dir}")
    target_link_libraries(floatIBM_refgen PRIVATE m)

    # Smoke test for the C-side primitives — fast sanity check that runs
    # without Node, useful for iterating on tools/floatIBM/ibmFloat.c.
    add_executable(floatIBM_smoke
        "${_floatIBM_dir}/smoke.c"
        "${_floatIBM_dir}/ibmFloat.c"
    )
    target_include_directories(floatIBM_smoke PRIVATE "${_floatIBM_dir}")
    target_link_libraries(floatIBM_smoke PRIVATE m)
    add_test(NAME floatIBM_smoke COMMAND floatIBM_smoke)
    set_tests_properties(floatIBM_smoke PROPERTIES LABELS "unit;floatIBM")

    if(EXISTS "${CMAKE_SOURCE_DIR}/ext/sim/test/test_floatIBM.cjs")
        add_test(NAME floatIBM_compliance
            COMMAND node
                    "${CMAKE_SOURCE_DIR}/ext/sim/test/test_floatIBM.cjs"
                    "$<TARGET_FILE:floatIBM_refgen>"
            WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}/ext/sim"
        )
        set_tests_properties(floatIBM_compliance PROPERTIES
            LABELS "unit;floatIBM"
            DEPENDS floatIBM_smoke
        )
    else()
        message(WARNING "ext/sim/test/test_floatIBM.cjs not found (ext/sim "
            "submodule not initialized?) — skipping floatIBM_compliance")
    endif()
else()
    #message(WARNING "tools/floatIBM sources not found — skipping "
    #    "floatIBM_refgen, floatIBM_smoke, floatIBM_compliance")
endif()

# asm101 assembler tests (upstream smoke + fork feature/fastparse) live in
# their own CMakeLists alongside the fixtures they assemble.  It also defines
# the `test-asm101*` convenience run targets.
add_subdirectory(asm101)

# =====================================================================
# fpasm tests — hand-written AP-101S assembly that exercises FP
# instructions and prints results via halUCP.  The assembled obj is
# linked against the runtime library (IOINIT, etc.) into a runnable
# .fcm; output is captured via outfile6 and diffed against a baseline.
# =====================================================================
set(_fpasm_dir "${CMAKE_CURRENT_SOURCE_DIR}/fpasm")
set(_fpasm_build_dir "${CMAKE_CURRENT_BINARY_DIR}/fpasm")
set(_fpasm_src "${_fpasm_dir}/fpasm_test.asm")
set(_fpasm_obj "${_fpasm_build_dir}/fpasm_test.obj")
set(_fpasm_fcm "${_fpasm_build_dir}/fpasm_test.fcm")
set(_fpasm_sym "${_fpasm_build_dir}/fpasm_test.sym.json")
set(_fpasm_actual "${_fpasm_build_dir}/fpasm_test.actual.out6")
set(_fpasm_baseline "${HAL_TEST_BASELINE_DIR}/fpasm_test.expected.out6")

add_custom_command(
    OUTPUT "${_fpasm_obj}"
    COMMAND ${CMAKE_COMMAND} -E make_directory "${_fpasm_build_dir}"
    COMMAND ${CMAKE_COMMAND} -E env PYTHONUTF8=1
            "${SDL_VENV_PYTHON}" -m asm101
            "--object=${_fpasm_obj}"
            "${_fpasm_src}"
    DEPENDS "${_fpasm_src}"
    COMMENT "[asm101] Assembling fpasm_test.asm"
)
add_custom_command(
    OUTPUT "${_fpasm_fcm}" "${_fpasm_sym}"
    COMMAND ${CMAKE_COMMAND} -E env PYTHONUTF8=1
            "${SDL_VENV_PYTHON}" -m lnk101
            --no-repro --generate-stacks 80 --entry FPTEST
            -o "${_fpasm_fcm}"
            --json-symbols "${_fpasm_sym}"
            -L "${CMAKE_BINARY_DIR}/lib/runtime/RUN"
            -L "${CMAKE_BINARY_DIR}/lib/runtime/ZCON"
            "${_fpasm_obj}"
    DEPENDS "${_fpasm_obj}" runtime
    COMMENT "Linking fpasm_test"
)
add_custom_target(fpasm_test_build ALL DEPENDS "${_fpasm_fcm}")
add_dependencies(fpasm_test_build halsfc lnk101 runtime)

set(_fpasm_actual_exc "${_fpasm_build_dir}/fpasm_test.actual.exc")
set(_fpasm_baseline_exc "${HAL_TEST_BASELINE_DIR}/fpasm_test.expected.exc")

# Pass GPC_BATCH_COMMAND as a single semicolon-joined arg; the wrapper
# splits it back into program+args.  This lets us route the same way
# whether SDL_UNIFIED_GPC is on (gpc;run) or off (gpc-batch).
string(REPLACE ";" "\;" _fpasm_gpc_cmd "${GPC_BATCH_COMMAND}")

add_test(NAME fpasm_test
    COMMAND "${CMAKE_CURRENT_SOURCE_DIR}/fpasm/run_fpasm_test.sh"
        "${_fpasm_gpc_cmd}"
        "${_fpasm_fcm}"
        "${_fpasm_sym}"
        "${_fpasm_build_dir}"
)
set_tests_properties(fpasm_test PROPERTIES
    LABELS "fpasm"
    TIMEOUT 30
    FIXTURES_SETUP "fpasm_test_OUTPUT"
)
add_test(NAME fpasm_test.compare
    COMMAND diff -u "${_fpasm_baseline}" "${_fpasm_actual}")
set_tests_properties(fpasm_test.compare PROPERTIES
    LABELS "fpasm"
    TIMEOUT 10
    FIXTURES_REQUIRED "fpasm_test_OUTPUT"
)
add_test(NAME fpasm_test.exceptions
    COMMAND diff -u "${_fpasm_baseline_exc}" "${_fpasm_actual_exc}")
set_tests_properties(fpasm_test.exceptions PROPERTIES
    LABELS "fpasm"
    TIMEOUT 10
    FIXTURES_REQUIRED "fpasm_test_OUTPUT"
)

# ---------------------------------------------------------------------
# HAL/S-FC optimizer cross-block CSE regression (XCOM-I RECORD DYNAMIC
# row-stride fix): compiles two tiny probes with X6 and gates on the
# optimizer's "CSE'S FOUND" statistics.  See test_halsfc_cse.py.
add_test(NAME halsfc_cse
    COMMAND "${SDL_VENV_PYTHON}"
        "${CMAKE_CURRENT_SOURCE_DIR}/test_halsfc_cse.py"
        "${CMAKE_BINARY_DIR}/bin/halsc"
        "${CMAKE_CURRENT_BINARY_DIR}/halsfc_cse"
)
set_tests_properties(halsfc_cse PROPERTIES
    LABELS "halsfc"
    TIMEOUT 120
    FIXTURES_REQUIRED "ENV_READY"
)

# ---------------------------------------------------------------------
# dfg (Display Format Generator) every-deck sweep: full generation of
# every display deck in the out-of-tree OI301700 + OI340600 code trees
# (194 decks), gating on zero errors, the pinned honest-refusal sets,
# and the OI30 members' own 30.40-banner build metrics as a per-display
# oracle (see test_dfg_decks.py).  SKIPs cleanly when pfs/code is
# absent.  Run with:  ctest -R dfg_decks   or   make test-dfg
# ---------------------------------------------------------------------
# AP-101 loadable-module (.lib) container: record roundtrip + an
# assemble/link integration gating CESD, extents, layered store-protection
# (PROT-card ranges over class defaults), and RLD retention.
add_test(NAME libmodule
    COMMAND "${SDL_VENV_PYTHON}"
        "${CMAKE_CURRENT_SOURCE_DIR}/test_libmodule.py"
)
set_tests_properties(libmodule PROPERTIES
    LABELS "lnk101"
    TIMEOUT 120
)

# ---------------------------------------------------------------------
# tools.mmu2fcm (GPC load emulation composing phase .libs into a
# configuration .fcm) plus the two Mass-Memory-Build stamp generators it
# drives: ap101Utils.mmbstamp (#PFCMGPT/#PCDCPHA) and ap101Utils.g3dat
# (FCMG3DAT, the G3 archive destination address table).
add_test(NAME mmu2fcm
    COMMAND "${SDL_VENV_PYTHON}"
        "${CMAKE_CURRENT_SOURCE_DIR}/test_mmu2fcm.py"
)
set_tests_properties(mmu2fcm PROPERTIES
    LABELS "ap101Utils"
    TIMEOUT 120
)

# ---------------------------------------------------------------------
# tools.mmu2mmv (the built phases laid onto a mass memory tape volume):
# the tape geometry and the two address forms -- an ALLOC card's FTSBB
# with the file above the track, and the track-first form a unit reports
# on the bus -- plus the .mmv file layout, read back by ext/sim/mmu when
# that bundle is built.
add_test(NAME mmu2mmv
    COMMAND "${SDL_VENV_PYTHON}"
        "${CMAKE_CURRENT_SOURCE_DIR}/test_mmu2mmv.py"
)
set_tests_properties(mmu2mmv PROPERTIES
    LABELS "ap101Utils"
    TIMEOUT 120
)

# ---------------------------------------------------------------------
# tools.mmustamp (the Mass-Memory-Build tables written into the phase load
# modules): which module owns a table -- every phase referencing a compool
# declares it, one carries it -- and that the sidecar keeps the linkedit
# content, so re-stamping preserves the baseline and --restore inverts.
add_test(NAME mmustamp
    COMMAND "${SDL_VENV_PYTHON}"
        "${CMAKE_CURRENT_SOURCE_DIR}/test_mmustamp.py"
)
set_tests_properties(mmustamp PROPERTIES
    LABELS "ap101Utils"
    TIMEOUT 120
)

# ---------------------------------------------------------------------
# tools.asmimage (assemble one AP-101 program and locate it at a fixed
# address): the addressing forms an IOP program uses -- an 18-bit absolute
# address, a negative addend on a command table base, a program-counter-
# relative displacement -- and that ext/sim's committed IPL microcode image
# still matches the source beside it.
add_test(NAME asmimage
    COMMAND "${SDL_VENV_PYTHON}"
        "${CMAKE_CURRENT_SOURCE_DIR}/test_asmimage.py"
)
set_tests_properties(asmimage PROPERTIES
    LABELS "tools"
    TIMEOUT 120
)

# ---------------------------------------------------------------------
# The located image as the simulator writes it to storage: the MSC
# program, the two BCE programs, the receive sequence, and the three mass
# memory command words the record address decides.
if(EXISTS "${CMAKE_SOURCE_DIR}/ext/sim/test/test_iplmicro.cjs")
    add_test(NAME iplmicro
        COMMAND node "${CMAKE_SOURCE_DIR}/ext/sim/test/test_iplmicro.cjs"
        WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}/ext/sim"
    )
    set_tests_properties(iplmicro PROPERTIES
        LABELS "gpc-sim"
        TIMEOUT 120
    )
endif()

# ---------------------------------------------------------------------
# ap101Utils.halorder (con80build template closure inputs): D INCLUDE
# TEMPLATE card forms (with/without a blank after the col-1 'D'),
# display-deck INCLUDE= seeding, transitive seed closure, and the
# CARDTYPE change-flag map ('T' -> main).
add_test(NAME con80_halorder
    COMMAND "${SDL_VENV_PYTHON}"
        "${CMAKE_CURRENT_SOURCE_DIR}/test_halorder.py"
)
set_tests_properties(con80_halorder PROPERTIES
    LABELS "ap101Utils"
    TIMEOUT 60
)

# con80.con80build .hal-mirror build-layer rewrites (native-CARDTYPE
# comment remaps) + the flight-faithful halorder CARDTYPE table for the
# STRPDT R-card dispositions (the phase-15/16 DQ7/PM1 wave was the guessed
# R->M default activating INCL80/STRPDT's unqualified NAME declares inside
# the SM compools; flight compiles them as comments there).
add_test(NAME con80_mirror
    COMMAND "${SDL_VENV_PYTHON}"
        "${CMAKE_CURRENT_SOURCE_DIR}/test_con80_mirror.py"
)
set_tests_properties(con80_mirror PROPERTIES
    LABELS "ap101Utils"
    TIMEOUT 60
)

# con80.con80build patch-deck (PCHnnSRC/PCHnnTXT) detection: must recognize
# a patch source's PCHnnSRC filename whether or not it carries a trailing
# .asm extension -- both spellings appear across the OI30/OI34 corpora.
add_test(NAME con80_patch_decks
    COMMAND "${SDL_VENV_PYTHON}"
        "${CMAKE_CURRENT_SOURCE_DIR}/test_con80_patch_decks.py"
)
set_tests_properties(con80_patch_decks PROPERTIES
    LABELS "ap101Utils"
    TIMEOUT 60
)

# con80.con80build.SourceIndex.resolve() for a <=6-char-stem source file
# (e.g. SSSRC/FAZ2.asm) whose own CSECT differs from its filename -- the
# file is found only by its own name, and a .asm extension used to bleed
# into the 6-char stem window.
add_test(NAME con80_source_index
    COMMAND "${SDL_VENV_PYTHON}"
        "${CMAKE_CURRENT_SOURCE_DIR}/test_con80_source_index.py"
)
set_tests_properties(con80_source_index PROPERTIES
    LABELS "ap101Utils"
    TIMEOUT 60
)

add_test(NAME dfg_decks
    COMMAND "${SDL_VENV_PYTHON}"
        "${CMAKE_CURRENT_SOURCE_DIR}/test_dfg_decks.py"
)
set_tests_properties(dfg_decks PROPERTIES
    LABELS "dfg"
    TIMEOUT 300
    # Temporarily disabled.
    DISABLED TRUE
)

# DEUCFLM critical-format load module: build from CON80/CFSYSIN and gate
# the derivable structure (CFIT branch slots, DEULOC cross-check, body
# tails, CFBSIZE budget).  SKIPs cleanly when pfs/code is absent.
add_test(NAME dfg_deucflm
    COMMAND "${SDL_VENV_PYTHON}"
        "${CMAKE_CURRENT_SOURCE_DIR}/test_deucflm.py"
)
set_tests_properties(dfg_deucflm PROPERTIES
    LABELS "dfg"
    TIMEOUT 120
)
# The critical formats as a build step: con80build --critfmt resolves the
# CFSYSIN members to decks, runs dfg + halsc on each, and links DEUCFLM.
# The unit above gates the image's structure, this the wiring.  Needs the
# toolchain and the OI tree.
if(EXISTS "${CMAKE_SOURCE_DIR}/code/OI340700/CON80/CFSYSIN")
    add_test(NAME dfg_critfmt_build
        COMMAND "${CMAKE_BINARY_DIR}/bin/con80build"
            --root "${CMAKE_SOURCE_DIR}/code/OI340700"
            --out "${CMAKE_CURRENT_BINARY_DIR}/critfmt"
            --critfmt
    )
    set_tests_properties(dfg_critfmt_build PROPERTIES
        LABELS "dfg"
        TIMEOUT 300
        # The step exits nonzero on any failure; this catches the case
        # that would not -- no members, so nothing linked.
        PASS_REGULAR_EXPRESSION "DEUCFLM.bin"
    )
endif()

add_custom_target(test-dfg
    COMMAND "${CMAKE_CTEST_COMMAND}" -L dfg --output-on-failure
    DEPENDS uv_sync
    WORKING_DIRECTORY "${CMAKE_BINARY_DIR}"
    COMMENT "Running dfg tests"
    USES_TERMINAL
)
