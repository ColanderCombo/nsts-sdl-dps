T        CSECT
* CNOP n aligns the counter to an n-halfword boundary.
* AFULL: CNOP 2 after a halfword -> fullword (halfword 2).
* BHALF: CNOP 1 is a no-op when already halfword-aligned -> halfword 3.
* The pad halfword(s) are filled with BCF 0,0 (0xD800), the AP-101 NOP, NOT
* zeros -- verified against 21 such "NOP" pads in DASS_SSW (golden carries D800).
* (A pad wider than 1 halfword is untested here; the flight has no such case.)
         DC    H'0'
AFULL    CNOP  2
         DC    H'0'
BHALF    CNOP  1
         END
