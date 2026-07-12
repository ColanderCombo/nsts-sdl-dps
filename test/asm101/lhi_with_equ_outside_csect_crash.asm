R6       EQU   6
$0MIN1   CSECT
M1       DS    0H
         ENTRY M1
         LHI   R6,10
         SVC   0,21
         END   M1
