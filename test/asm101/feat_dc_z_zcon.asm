* Regression: DC Z(,sym,flags) ZCON must assemble and emit a Z-type RLD
* (flag 0x04) against the external symbol, with the ZCON flags byte placed in
* the data image at byte 2 ([0, 0, flags, 0]).  No .asm fixture exercised the
* full ZCON assemble->object path, so a broken appendReloc call on it went
* undetected; this guards that path end to end.
ZCONT    CSECT
         EXTRN EXTSYM
ZREF     DC    Z(,EXTSYM,X'8')
         DS    F
         END
