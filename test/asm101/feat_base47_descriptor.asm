* RS forceRS ops with an explicit base register 4-7.  The RS extended (AM=0) form
* carries a 3-bit base in the first halfword's low bits; for base 4-7 the high bit
* lands in the slot the descriptor models as the AM ('a') bit (base 0-3 leaves it
* 0, i.e. AM=0).  This exercises the rs_hw1 base-4-7 path (the last case that used
* the legacy argsSRSorRS opcode fallback); byte-identical to that packing.
BASE47   CSECT
         LPS   X'48'(4)
         AST   3,X'10'(5)
         LXA   2,X'20'(6)
         STXA  3,X'30'(7)
         BAL   4,X'40'(5)
         BCT   5,X'50'(6)
         MIH   1,X'60'(4)
         IHL   2,X'70'(5)
         SST   3,X'08'(7)
         END
