         MACRO
&N       RDLEN &S
.* Mimics the POS macro: read back the length attribute of a position-
.* defined symbol (set by a 3-operand EQU, as PDEF emits) and recover the
.* encoded coordinate as &L = L'sym - 1025, then materialize it as data.
         LCLC  &#
         LCLA  &L
&#       SETC  '&S'
&L       SETA  L'&#-1025
&N       DC    F'&L'
         MEND
T        CSECT
FOO      EQU   5,5+1025,C'@'        length attribute = 1030, like &N.X
RESULT   RDLEN FOO                  DC F'5'  (L'FOO-1025 = 1030-1025)
         END
