T        CSECT                                                          0000010A
         BALR  R1,0                                                    0000020A
         USING *,R1                                                    0000030A
         TB    OK,X'88'
OK       DC    H'136'
R1       EQU   1
         END
