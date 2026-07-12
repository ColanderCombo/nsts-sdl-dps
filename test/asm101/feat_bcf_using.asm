T        CSECT
R1       EQU   1
* Forward BC under a covering USING must encode a PC-relative SRS (BCF)
* displacement, NOT the target's displacement from the USING base register.
* USING T,R1 makes findB2D2 resolve every local target to base register R1,
* which routed forward BC through the generic SRS path -- where d2 had been
* reassigned to the target's displacement from the base register and emitted
* as the SRS displacement.  An SRS branch is PC-relative; the active USING
* base is irrelevant to it.  Before the fix C1 encoded as DF08 (disp 2,
* reaching halfword 3) and C2 as DA14 (disp 5, reaching halfword 8) -- both
* overshooting their targets while still "assembling".  This is exactly the
* IFPROC BC mask,#@LBn miscompile in IFTESTS, whose USING *,R12 with
* R12 EQU 1 is the same covering-USING idiom.
* C1 at hw0 -> FWD1 at hw2: PC-relative disp 1 -> DF04.
* C2 at hw2 -> FWD2 at hw5: PC-relative disp 2 -> DA08.
         USING T,R1
C1       BC    07,FWD1
         DC    H'0'
FWD1     DS    0H
C2       BC    02,FWD2
         DC    H'0'
         DC    H'0'
FWD2     DS    0H
         END
