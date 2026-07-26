         MACRO
         BENT  &A,&ANNUN,&ERTBL
&A       DC    XL.8'&ANNUN',YL.8(&ERTBL-TAB)
         MEND
         MACRO
         DRIVE
&ERRLBL   SETC  'FOO'
.* The invocation's FIRST card holds no '&' at all -- the variable rides the
.* continuation card (FCMBMTMC's `BMTENT ...,` / `  0C,&AERRLBL` shape), and
.* the expansion's FIRST statement is the generated DC itself (nothing
.* interleaves to absorb the dangling continuation flag in codegen).
         BENT  X1,0C,                                                  C
               &ERRLBL
         MEND
TEST     CSECT
TAB      DS    H
FOO      DS    H
         DRIVE
         END
