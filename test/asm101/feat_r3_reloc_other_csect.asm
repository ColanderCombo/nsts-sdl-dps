* An explicitly coded R3 is 'NO BASE' -- so the
* AM=0 16-bit field holds an absolute address and a relocatable
* displacement over it must carry a Y RLD naming the csect that contains
* the target
Z3       EQU   3
ONE      CSECT
         LA$   1,TABLE(Z3)        -> RLD naming TWO
         LA$   2,LOCAL(Z3)        -> RLD naming ONE
         LA    4,LOCAL(0)         a REAL base register: no RLD
LOCAL    DC    H'0'
TWO      CSECT
TABLE    DS    0H
         DC    H'1'
         DC    H'2'
         END
