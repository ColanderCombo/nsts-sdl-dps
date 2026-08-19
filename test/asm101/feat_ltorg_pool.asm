* Literal-pool mechanics:
* (a) a mid-csect LTORG occupies its pool's size, so following statements
*     start past it
* (b) the end-of-source pool origin is fullword-aligned, not aligned to
*     its widest literal
* (c) a =Y literal over a relocatable value carries a pool-slot Y RLD
*
LP       CSECT
R1       EQU   1
R5       EQU   5
         EXTRN EXTV
         N     R5,=X'000007FF'
         LTORG
ZC1      DC    Z(,EXTV,0)
         CH    R1,=Y(LOCLBL)
         CH    R1,=Y(EXTV+8)
         BR    R5
LOCLBL   DS    0H
         END
