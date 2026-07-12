T        CSECT
R2       EQU   2
R4       EQU   4
R7       EQU   7
* Branch on Count, backward target -> BCTB (SRS): 11011 rrr dddddd 11.
* disp = (instr_halfword + 1) - target; hardware: target = NIA - disp.
* BCT is in argsRSonly, which forced the RS path and suppressed BCTB,
* mis-encoding every backward BCT (e.g. BCT R4,X -> garbage, not BCTB).
* It now uses BCTB unless the `$` long format is explicitly forced.
B1       DC    H'0'                 B1 at halfword 0
         DC    H'0'                 halfword 1
C1       BCT   R4,B1                disp 3 -> DC0F
B2       DC    H'0'                 halfword 3
         DC    H'0'                 halfword 4
C2       BCT   R2,B2                disp 3, R2 -> DA0F
B3       BCT   R7,B3                self, disp 1, R7 -> DF07
         END
