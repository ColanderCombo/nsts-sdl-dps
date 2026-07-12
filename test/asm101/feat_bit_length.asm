T        CSECT
* DC bit-length ("L.n") packing: suboperands pack into a contiguous
* MSB-first bit stream, zero-padded to the next byte boundary.
* PACK is the pinned reference: YL.2(3),YL.7(5),YL.7(6) ->
*   11 0000101 0000110 -> 0xC286 (verifies bit order + padding).
PACK     DC    YL.2(3),YL.7(5),YL.7(6)
MIXB     DC    BL.5'10010',FL.11'5'
MIXA     DC    AL.16(SYM),X'0001'
SYM      DS    0H
END_     DS    0H
         END
