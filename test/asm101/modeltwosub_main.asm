* Two-subscript &SYSLIST(i,j) used DIRECTLY in a model statement (the
* DOPROC `LR &SYSLIST(&I,1),&SYSLIST(&I,2)` shape).  Called with the
* sublist (R2,R3): operand 1 element 1 = R2, element 2 = R3, so the
* expansion is `LR R2,R3` = 1AE3.  Before the svReplace two-subscript
* fix, nameSet0 dropped the (1,1)/(1,2) and &SYSLIST rendered as its
* whole nested arg list (Python repr) with "(1,1)" left as text, so the
* model statement failed to parse and emitted nothing.
MSUB     CSECT
         MODELSUB (R2,R3)
R2       EQU   2
R3       EQU   3
         END
