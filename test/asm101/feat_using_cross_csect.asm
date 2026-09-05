* A USING covers one control section.  A reference from code under
* `USING TAB,R1` (TAB in CSECT A) to a label in CSECT B takes the no-base
* form with the label's address and a relocation against B, whatever the
* displacement: OI301700 BILDNEW5's `STH R3,MSG257A` under `USING STM4,R0`
* names a field of the LINES CSECT and is BBF3 504D.  TAB+4 in the same
* CSECT takes the base.
A        CSECT
R1       EQU   1
R4       EQU   4
         USING TAB,R1
TAB      DS    10H
L0       L     R4,BLAB
L1       L     R4,TAB+4
L2       LA    R4,BFAR
B        CSECT
         DS    20H
BLAB     DS    H
         DS    6000H
BFAR     DS    H
         END
