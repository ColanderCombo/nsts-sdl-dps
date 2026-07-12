# AP-101 macro overrides for the MLIB80 *TEST* corpus

The MLIB80 `*TEST*` decks (`../`) exercise the AP-101 macro system, but a few
MLIB80 macros were written System/360-style and emit instructions that do not
exist on the AP-101.  Rather than edit the read-only pfs originals, this
directory holds local copies of those macros with the /360 instructions
replaced by AP-101 substitutes.  Pass it FIRST on the library search path so it
shadows the pfs copies (first match wins):

```
build/venv/bin/python -m asm101 test/asm101/mlib80_tests/<DECK> \
    -L test/asm101/mlib80_tests/ap101_overrides \
    -L ../pfs/code/OI340600/MLIB80 -l /tmp/x.lst
```

## DOPROC

The DO macro's loop-control helper emits the /360 index branches `BXLE`
(branch on index low-or-equal) and `BXH` (branch on index high), which take
three operands `R1,R3,D2`.  The AP-101 has no such instruction; its index
branch is `BIX` (branch on index), a 2-operand RS branch `R1,D2` — the same
shape the macro already uses for its `BCT` loop ends (`.FONLY`/`.BCTRZ`).

The five `PUSHINS (BXLE/BXH/&P1, …)` sites (the `.GENBXLE`/`.GENBXH`/`.BCTR1`
hardcoded paths and the `.PFT`/`.PFB` explicit-`&P1` passthroughs) are changed
to `PUSHINS (BIX,&FROM(1),&LIND(&LI))`, dropping the increment-register operand
the AP-101 form doesn't take.  We only care that the macro system OPERATES, not
that the loop is functionally faithful, so the dropped increment register is
acceptable.

⚠️ Column 72 is the continuation column: keep edited macro lines ≤ 71 chars (do
not let a trailing sequence number land on col 72, or the line "continues" into
the next and swallows the following MEXIT — which manifests as a runaway
conditional-assembly loop).
