asm101 assembler regression specimens
======================================

Tiny standalone .asm fixtures used to nail down specific behaviors of the
asm101 assembler fork (src/asm101/). Most of them exercise RLD
(relocation dictionary) emission for various EXTRN-referencing instruction
forms, which were fixed across several rounds in
`src/asm101/model101.py`.

The original RLD specimens (below) are not wired into ctest; assemble manually:

    venv/bin/python -m asm101 --object=/tmp/foo.obj test/asm101/<name>.asm

Each file is a behavior specimen — preserve byte-for-byte. Don't reformat or
"clean up". A few are expected to FAIL to assemble (deferred features); those
are flagged below.

Automated feature/regression suite
----------------------------------

`test/test_asm101_features.py` (ctest target `asm101_features`) drives the
fork automatically.  It has a UNIT layer (imports the parser/evaluator and
asserts on tiny inputs) and an INTEGRATION layer (assembles the `feat_*.asm`
fixtures here and asserts on exit status / listing).  Run it with:

    ctest -R asm101_features
    build/venv/bin/python test/test_asm101_features.py    # standalone

The `feat_*` fixtures and the `maclib_*/` + `*_main.asm` pairs are consumed by
that suite; keep them in sync with the assertions in the driver.

feat_dc_dup.asm          `DC 5F'0'` — duplication factor (was an infinite loop).
feat_dc_y_x.asm          `DC Y(SYM),X'11'` — multiple suboperands of mixed type.
feat_bare_drop.asm       bare `DROP` (release all base registers; no operand).
feat_long_format.asm     `B` vs `B$` — '$' forces the long (RS) instruction form.
feat_rs_empty_index.asm  `LED@ F0,0(,B2)` — RS operand with an omitted index.
feat_equ_multi.asm       `EQU 1,1,C'#'` — value,length,type operand form.
maclib_actr/LOOPER       infinite AIF/AGO macro; actr_main.asm trips the ACTR cap.
maclib_scan/MYMAC,PROGDECK   macro file beside a non-macro deck; scan_main.asm
                         must use the macro and ignore the deck.

Files
-----

bal_extrn_emits_rld.asm
    BAL R7,EOUT against EXTRN EOUT. Was case #4 of the RLD-emission fix.

bare_csect_dc.asm
    Minimal valid input: bare CSECT + `DC X'1234'`. Sanity baseline.

dc_y_with_extrn.asm
    `DC Y(@0M7)` referencing an EXTRN. Exercises Y-DC EXTRN handling.

equ_inside_csect.asm
    `R6 EQU 6` inside a CSECT then `LHI R6,10`. Works.

equ_outside_csect_crashes.asm
    Same as equ_inside_csect.asm but with the EQU placed *before* the CSECT.
    Used to crash with KeyError; specimen for that bug class.

lhi_constant_no_rld.asm
    LHI with plain integer / hex constants (no EXTRN). Sanity check that the
    RLD-emission fix did not regress the no-relocation path.

lhi_extrn_emits_rld.asm
    `LHI R0,@0M6` against EXTRN @0M6. Was case #1 of the RLD-emission fix.

lhi_literal_register.asm
    `LHI 6,10` with a literal register number (no `R6 EQU 6`). Works.

lhi_with_equ_outside_csect_crash.asm
    Ultra-minimal `LHI` test that explored the crash-on-`R6 EQU 6`-before-CSECT
    behavior. Predecessor to equ_outside_csect_crashes.asm.

scal_at_pound_one_index_unsupported.asm
    `SCAL@# R0,#QEOUT(R3)` — indirect-via-ZCON form with one index.
    DEFERRED: currently fails ("Could not interpret line as SRS or RS").

scal_at_pound_two_index_unsupported.asm
    `SCAL@# R0,#QEOUT(R1,R3)` — indirect-via-ZCON form with two indices.
    DEFERRED: currently fails (same diagnostic as the one-index variant).

scal_no_base_control.asm
    `SCAL R0,#QEOUT` (no base register). Already worked before the RLD fix;
    serves as the control case alongside scal_with_base_emits_rld.asm.

scal_with_base_emits_rld.asm
    `SCAL R0,#QEOUT(R3)` with explicit base. Was case #5 of the RLD fix.
