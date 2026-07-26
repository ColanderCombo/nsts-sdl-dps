* A $-forced branch to a label STRICTLY INSIDE the csect takes RS AM=0
* with an absolute 16-bit target -- which must carry a Y-type RLD naming
* the csect, or the linked image branches to the unrelocated offset
* (flight links FCMISYNC's 'BNZ$ FCMNOIOS' as C3F3 8EFE = base+offset).
* A label at the very END of the csect rides a different path that always
* emitted the RLD (feat_long_format); this fixture pins the inside case.
T        CSECT
R0       EQU   0
         B$    INSIDE
         DS    2H
INSIDE   DS    0H
         LR    R0,R0
         END
