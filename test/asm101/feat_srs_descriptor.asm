* Exercises the CPU SRS short-form path, which is encoded from the auto-
* extracted GPC descriptors (instrdefs.CPU via generateSRS) -- the Stage-3a SRS
* slice.  Covers every "standard" x/d/b RS/SRS mnemonic whose descriptor
* encoding was verified equal to the legacy bit-packing, so the byte-golden test
* guards the descriptor path.  Small displacement + base 0-3 keep every line in
* the 2-byte short form; M/D and the floating mnemonics use an even R1.
SRSTEST  CSECT
         A     3,4(2)
         AH    3,4(2)
         C     3,4(2)
         CH    3,4(2)
         D     2,4(2)
         L     3,4(2)
         LA    3,4(2)
         LH    3,4(2)
         M     2,4(2)
         MH    3,4(2)
         N     3,4(2)
         O     3,4(2)
         S     3,4(2)
         SH    3,4(2)
         ST    3,4(2)
         STH   3,4(2)
         X     3,4(2)
         IAL   3,4(2)
         AE    2,4(2)
         DE    2,4(2)
         LE    2,4(2)
         ME    2,4(2)
         SE    2,4(2)
         STE   2,4(2)
         END
