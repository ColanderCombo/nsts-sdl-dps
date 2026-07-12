* ORG sets the location counter within the section.  Backward `ORG *-1`
* (as the message-table macros use after an FCW2 control halfword) lets the
* next statement reuse/overwrite the previous halfword.  Here B is at
* halfword 1; after the two DCs the counter is at halfword 2; `ORG *-1` backs
* it to halfword 1, so C is defined at B's address and its data overwrites B.
* Bug history: the operand `*-1` was dropped during loading (the operand-skip
* test checked the MINIMUM operand count, which is 0 for the optional-operand
* ORG), so ORG silently became a no-op and C landed at halfword 2.
ORGTST   CSECT
A        DC    H'1'
B        DC    H'2'
         ORG   *-1
C        DC    H'3'
         END
