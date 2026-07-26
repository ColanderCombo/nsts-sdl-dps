* N' of a nested-sublist macro argument must count the sublist's top-level
* elements.  Nested sublists are re-rendered to their source text "(a,b,...)"
* when subscripted out of &SYSLIST, and N' used to see only a scalar string
* and answer 1 -- MLIB80 TFBCD's .BCELOOP then built every multi-bus FIOBCD
* mask with only the first bus bit (flight/OI30 TBCD0079 = 00000E00, ours
* 00000800).
         MACRO
         TNSUB &A
&N1      SETA  N'&SYSLIST(1)
&N2      SETA  N'&SYSLIST(1,2)
&N3      SETA  N'&SYSLIST(1,3)
         DC    AL1(&N1)
         DC    AL1(&N2)
         DC    AL1(&N3)
         MEND
TESTN    CSECT
         TNSUB (3,(7,8,9),291,XYZ)
         END
