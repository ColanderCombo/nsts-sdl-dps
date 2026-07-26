* A bare paren register over a RELOCATABLE displacement is an INDEX
* (base comes from the covering USING) for ANY register -- R0-R3
* included, not just the R4-R7/@# heuristic: flight FCMDSCRM +00F1
* encodes 'LH R5,TDWASBT(R3)' under 'USING TFDWA,R0' as 9DF4 6004
* (AM=1, x2=R3, b=R0, d=4) where R3-as-base emitted 9DF3 0004 --
* wrong runtime EA (same-deck '(R5)'/'(R7)' already converted, only
* R0-R3 diverged).  FPMERLOG/FPMEVENQ 'ST R7,TAUXPSW(R3)' -> 37F4 6014
* likewise.  The ABSOLUTE-displacement case keeps the register as base
* (FCMISYNC 1CF3 0088 -- see feat_r3_base_absolute).
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
* (flight FCMTRACE +0005 'BL$ FCMWRAP(R3)' assembles C2F3 001C and
* links 98BC relocated; R3-as-base suppressed the RLD).
         BL$   TARG(R3)
TARG     DS    0H
         BR    R7
* LA is EXEMPT from the index conversion: its paren register is the
* BASE even over a relocatable displacement -- IBM OI30 BILDNEW5
* 'LA$ B1,STM4(Z3)' / 'LA$ R5,STERROR(Z3)' assemble E9F3 14DA /
* EDF3 180C (AM=0 based form, INTHNDLR macro, 5 sites).
         LA    R5,TARG(R3)
TFDWA    DSECT
TDWAHD   DS    4H
TDWASBT  DS    6H
         END
