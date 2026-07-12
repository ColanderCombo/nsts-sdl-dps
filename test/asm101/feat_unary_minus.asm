T        CSECT
* Unary / doubled leading minus.  POS emits "XPOS -&L" for a negative
* coordinate &L, so the operand text reaches the assembler as "--285",
* which must fold to +285 in both arithmetic (EQU) and DC F/H value contexts.
NEG      EQU   --285                value folds to +285
PACK     DC    BL.5'10000',FL.11'--285'   5-bit 10000 + 11-bit 285 = 0x811D
SYM      DS    0H
         END
