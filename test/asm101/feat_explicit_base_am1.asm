* An EXPLICITLY-CODED base register with an expression displacement must
* encode RS AM=1 (11-bit displacement, base in b), matching the flight
* assembler's CASEN computed-goto idiom 'LH R2,#@LBn-*-3(,R2)' -> 9AF6.
* A USING-resolved base takes AM=0 (B9F2, TICCSYTM precedent) instead.
* The table sits past the SRS range so the short form can't absorb it.
TEST     CSECT
R2       EQU   2
         LH    R2,TBL-*-3(,R2)
         LH    R2,NEAR-*-3(,R2)
         DS    4H
NEAR     DC    Y(9)
         DS    70H
TBL      DC    Y(1)
         DC    Y(2)
         END
