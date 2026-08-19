* An IOP long-format 'a' (18-bit main-store address) field spans both
* halfwords: a[17:16] in hw1's low bits, a[15:0] in hw2.  A relocatable
* target must therefore carry a fullword ACON RLD over the whole 32-bit
* instruction -- a 2-byte reloc on hw2 alone drops address bit 16 at link
TESTIOP  CSECT
TGT      DC    Y(0)
GO       #BU   TGT
RD       #RDL  TGT
         END
