A        CSECT
* Forward reference to an EQU symbol defined LATER.  The preliminary scan
* skips EQU, so Y(LATEEQU) cannot resolve in pass 1 and logs a transient
* "Cannot evaluate Y-type constant" -- but it resolves once the EQU is
* processed, so the FINAL pass is clean.  Intolerable-error counting is
* judged at the final pass, so this assembles (it would spuriously abort
* under the old accumulate-across-all-passes counting).  RESULT pins the
* recovered value: Y(LATEEQU) == Y(TARGET) == halfword 1.
EARLYREF DC    Y(LATEEQU)
TARGET   DS    0H
LATEEQU  EQU   TARGET
         END
