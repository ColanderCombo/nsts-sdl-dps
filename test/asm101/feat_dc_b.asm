* Regression: DC B'...' (binary constant).  Previously a no-op stub that
* emitted NO bytes and did not advance the location counter, so the constant
* collapsed to zero width and the next item overlapped it.  Real flight data
* uses it heavily (BILDNEW5/GPCIPL JOBTABLE = 27 32-bit minor-cycle masks that
* the stub silently dropped).  Bits pack MSB-first; implicit length rounds up
* to a halfword (matching the X branch); an explicit BLn fixes the byte width.
DCB      CSECT
M32      DC    B'00000100001000000000000001000000'
W8       DC    B'10100101'
L2       DC    BL2'101'
DUP      DC    2B'1111'
AFTER    DC    X'AA'
         END
