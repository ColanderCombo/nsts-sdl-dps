# MLIB80 structured-macro test decks (AP-101 adaptations)

Local **copies** of the `*TEST*` driver decks from
`../pfs/code/OI340600/MLIB80/`, used to exercise the asm101 macro system
(IF/ELSE/ENDIF, DO, CASE, STRTSRCH, PROC, and the extended nested forms).
The originals are flight-test source and are **never modified** — only these
copies are edited.

Assemble with the macro library pointed at the original dir, e.g.:

```
build/venv/bin/python -m asm101 test/asm101/mlib80_tests/IFTESTS \
    -L ../pfs/code/OI340600/MLIB80 -l /tmp/x.lst
```

## AP-101 adaptations applied

These decks were written 360-style. The AP-101 has only **8** general
registers (R0–R7; the 16 GPRs are two PSW-swapped banks, only 0–7 valid),
so the decks' phantom high registers — which the authors had already
remapped to valid GPRs via `EQU` — were renamed to the GPR they map to and
the remap `EQU`s dropped/renamed:

| deck      | rename                                  |
|-----------|-----------------------------------------|
| CASETEST  | R12→R1                                   |
| DOTESTS   | R12→R1                                   |
| IFTESTS   | R12→R1                                   |
| XTENTEST  | R12→R1                                   |
| SRCHTEST  | R12→R0, R8→R1, R9→R2 (R9 dup `EQU` dropped) |
| PROCTEST  | (none — already uses valid REG0–REG7)    |

The instruction mnemonics in the macro conditions (AR/CR/CH/LR/NR/C/CIST/
TB/TRB/LACR/AHI/…) are all valid AP-101 instructions; no instruction
substitutions were needed.

`TEST` (a macro *definition*) and `TESTING` (a GPCIPL data-table fragment
using `$PON`/`$POF`/`TITL` print-control and undefined externs) are **not**
macro-system driver decks; they are copied for completeness but are not
adaptation targets.

## Status (toward assembling)

Register rename eliminated the `Bad register number` errors. **5 of 6 decks
now assemble clean (rc=0)**; only PROCTEST remains:

- **IFTESTS / CASETEST / XTENTEST / SRCHTEST / DOTESTS** — rc=0.
  - DOTESTS was unblocked by rendering nested-sublist macro args
    (`DO FROM=((R3),(R5))`); see [[asm101_nested_sublist_macarg_bug]].
  - SRCHTEST/XTENTEST emit `(TRB,(R0),…)`-style structured-IF forms whose
    register operand is a parenthesized expression `(R0)`; the assembler now
    accepts that (matching IBM AS037F1 IEUF8M→IEUF8V, which evaluates every
    register field as an expression). These obscure forms have no
    ground-truth object-code oracle — rc=0 + plausible encodings is the bar.
- **PROCTEST** — still rc=1: PROC-macro MNOTEs (RETURN REGISTER / PROCEDURE
  NAME). Pre-existing macro-system gap, next target.
