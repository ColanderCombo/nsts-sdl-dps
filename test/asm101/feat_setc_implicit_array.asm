         MACRO
         GENMASK &N
.* FCMBMTMC shape: subscripted SET symbols carry NO LCLC/GBLC declaration --
.* `&X(k) SETx` implicitly declares a LOCAL array, grown on write past its
.* current size (incl. the multi-value consecutive-element form).
&MASK(1)  SETC  'E000'
&MASK(2)  SETC  'F000','F800'
&HRIDX    SETA  &N
MASK&N   DC    X'&MASK(&HRIDX)'
         MEND
TEST     CSECT
* Element 1 (implicit declaration), then element 3 (multi-value growth).
         GENMASK 1
         GENMASK 3
         END
