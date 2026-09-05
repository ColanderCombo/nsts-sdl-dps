* An RS operand under a USING takes that base register and the
* displacement from it whenever the displacement fits the AM=0 form's
* 16-bit field.  OI301700 BILDNEW5 under `USING FAILDATA,B0` (B0 EQU 0):
* `LA R4,STMWAIT` = ECF0 1988, `L R6,BUMPWRDN` = 1EF0 17C2,
* `ST R7,BUMPWRDN` = 37F0 17C2, with the operands 0x1988 and 0x17C2
* halfwords past FAILDATA.
WIDE     CSECT
R0       EQU   0
R4       EQU   4
R6       EQU   6
R7       EQU   7
         USING BASE,R0
BASE     DS    0H
         DS    6082H
BUMPWRDN DS    H
         DS    453H
STMWAIT  DS    H
L0       LA    R4,STMWAIT
L1       L     R6,BUMPWRDN
L2       ST    R7,BUMPWRDN
         END
