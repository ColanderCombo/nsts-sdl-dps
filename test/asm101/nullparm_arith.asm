         MACRO
         NULLP &A,&B,&C
         AIF   (&B NE 0).NZ
         DC    X'0B0B'
         AGO   .BDONE
.NZ      DC    X'B1B1'
.BDONE   AIF   (&C EQ 2).CTWO
         DC    X'0C0C'
         AGO   .CDONE
.CTWO    DC    X'C2C2'
.CDONE   MEND
NULLPA   CSECT
* Explicitly-empty positional mid-list (the ,, form): null value, arith 0.
         NULLP X,,2
* Fully omitted trailing operands: also null, also arith 0.
         NULLP X
* Supplied values still compare normally.
         NULLP X,1,2
         END
