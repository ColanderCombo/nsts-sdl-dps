* Exercises the CPU RI and SI immediate forms, whose first halfword is now
* encoded from the auto-extracted GPC descriptors (instrdefs.CPU) -- the Stage-3a
* RI/SI slice.  Each is a 16-bit descriptor-encoded halfword ('...yyy/I' for RI,
* '...ddddddbb/I' for SI) followed by a 16-bit immediate.  Covers every RI/SI
* mnemonic whose descriptor encoding was verified equal to the legacy packing.
* (LHI is excluded: it is syntactically RI but assembles as the RS instruction LA.)
RISI     CSECT
         AHI   3,100
         CHI   3,100
         MHI   3,100
         NHI   3,100
         XHI   3,100
         OHI   3,100
         TRB   3,100
         ZRB   3,100
         CIST  4(2),100
         MSTH  4(2),100
         NIST  4(2),100
         XIST  4(2),100
         SB    4(2),100
         TB    4(2),100
         ZB    4(2),100
         TSB   4(2),100
         END
