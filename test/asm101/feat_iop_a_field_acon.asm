* An IOP long-format 'a' (18-bit main-store address) field spans both
* halfwords: a[17:16] in hw1's low bits, a[15:0] in hw2.  A relocatable
* target must therefore carry a fullword ACON RLD over the whole 32-bit
* instruction -- a 2-byte reloc on hw2 alone drops address bit 16 at link
* (flight FIOCBLKS 'FIOBRCFA #BU FIOBADFA' = F001 D91A, target 1D91A).
* #RDL's descriptor mislabeled its 18-bit address field 'c', so the
* relocatable-operand RLD was never emitted (FIODEUPG '#RDL FIOWCE' ->
* FB00 0000 vs flight FB00 8C94); it is an 'a' field like #TDL's.
TESTIOP  CSECT
TGT      DC    Y(0)
GO       #BU   TGT
RD       #RDL  TGT
         END
