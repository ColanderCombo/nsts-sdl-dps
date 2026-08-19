* The SRS short form's D field is six bits, but the original assembler 
* never spent all of it on a memory reference; they run 0..54
* a halfword store whose USING displacement comes to exactly 55 
* halfwords is emitted in the RS long form.
*
SRSMC    CSECT
R2       EQU   2
B1       EQU   1
         USING PLATE,B1
         STH   R2,AT54             D=54: SRS, one halfword
         STH   R2,AT55             D=55: RS long, two halfwords
SPAN     DS    0H                  halfword 3 -- 1 + 2, not 1 + 1 or 2 + 2
PLATE    DS    0H
         DS    54H
AT54     DS    1H                  PLATE+54
AT55     DS    1H                  PLATE+55
         END
