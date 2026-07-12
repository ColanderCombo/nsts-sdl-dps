* Instruction-operand literals (=H/=F/=E/=Y), ported in fastparse
* _lconstant.  Only =C/=X/=B were ported before.  A USING base register
* covers the section so each literal resolves to a pool displacement; the
* LTORG places the pool.  The literal VALUES (pool bytes) are pinned by
* the UNIT assertions in unit_tests() -- this fixture only proves the full
* parse->place->address pipeline assembles cleanly (rc==0).
T        CSECT
         BALR  R1,0
         USING *,R1
LBEG     DC    X'AAAA'
LSYM     DC    X'BBBB'
LEND     DC    X'CCCC'
         LH    R2,=H'58'
         L     R3,=F'64'
         CE    FR4,=E'1.0'
         LH    R5,=Y(LSYM)
         LH    R6,=Y(LEND-LBEG)
         LTORG
R1       EQU   1
R2       EQU   2
R3       EQU   3
R4       EQU   4
R5       EQU   5
R6       EQU   6
FR4      EQU   4
         END
