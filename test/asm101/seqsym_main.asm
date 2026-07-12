* Computed AGO + backward jump to a forward-skipped sequence symbol.
* CAGO uses a computed AGO `(&N).P1,.P2,.P3` to dispatch on &N, and its
* &N=2 path is `.P2 AGO .MID` (forward, SKIPPING .EMIT) then `.MID AGO .EMIT`
* (BACKWARD to the skipped .EMIT).  This exercises the two macro-engine fixes:
*   - computed AGO (the operand `(&N).seq1,...` was taken as one bogus target);
*   - sequence-symbol pre-scan (a backward jump to a label an earlier forward
*     jump skipped over was never recorded, so it ran off the macro end -- the
*     root of DCHAR dropping the data byte for every blank message character).
* Expected: C1 DC H'11'=000B, C2 DC H'22'=0016 (via the backward path), C3
* DC H'33'=0021.
SEQT     CSECT
C1       CAGO  1
C2       CAGO  2
C3       CAGO  3
         END
