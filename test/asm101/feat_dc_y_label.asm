* A DC's label names the START of its data (its first suboperand).  Each
* DC suboperand type handler calls commonProcessing, which assigns the
* label to the current location, so for a multi-suboperand DC the label
* used to be re-assigned at every suboperand and land on the LAST one.
* For `PTR DC Y(SYM),X'2'` that put PTR on the trailing X byte (an odd
* address) instead of the leading Y halfword; an indirect reference to
* PTR then resolved one halfword high.  PTR must sit on the Y: SYM is 4
* bytes (halfwords 0-1), so PTR is at halfword 2, not 3.
T        CSECT
SYM      DC    X'AAAAAAAA'
PTR      DC    Y(SYM),X'2'
NXT      DC    Y(SYM)
         END
