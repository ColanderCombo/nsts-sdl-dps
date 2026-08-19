* A bare paren register over a relocatable displacement is an index
* (base comes from the covering USING) for any register 
T4       CSECT
R0       EQU   0
R3       EQU   3
R5       EQU   5
R7       EQU   7
         USING TFDWA,R0
         LH    R5,TDWASBT(R3)
         ST    R7,TDWASBT(R3)
         DROP  R0
* No covering USING: the reference is current-section self-relative;
* the $-forced AM=0 form has no index field, so the register is
* dropped -- but the 16-bit absolute displacement keeps its Y RLD
         BL$   TARG(R3)
TARG     DS    0H
         BR    R7
* LA is exempt from the index conversion: its paren register is the
* base even over a relocatable displacement
* ...but the 16-bit field is an absolute address, so it takes a Y RLD
* like the BL$ above: an explicitly coded R3 is 'no base' too.
         LA    R5,TARG(R3)
TFDWA    DSECT
TDWAHD   DS    4H
TDWASBT  DS    6H
         END
