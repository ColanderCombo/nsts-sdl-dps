$0M7     CSECT
R0       EQU   0
         EXTRN @0M7
         LH    R0,ADRSTK
         B     END
ADRSTK   DC    Y(@0M7)
END      DS    0H
         END
