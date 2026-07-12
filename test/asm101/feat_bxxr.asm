T        CSECT
* Register-form conditional branches (extended mnemonics for BCR): the implied
* R1 is the condition mask, the operand is the R2 register.  None of these carry
* a descriptor of their own; model101 routes them through BCR's descriptor with
* the implied mask in R1, so the byte-golden guards that BCR-routing path for the
* whole family (was "Unrecognized line" before the branchAliasesR fix).
         BR    R7
         NOPR  R7
         BER   R7
         BHR   R7
         BHER  R7
         BLR   R7
         BLER  R7
         BMR   R7
         BNR   R7
         BNER  R7
         BNHR  R7
         BNLR  R7
         BNMR  R7
         BNNR  R7
         BNPR  R7
         BNZR  R7
         BPR   R7
         BZR   R7
R7       EQU   7
         END
