* Exercises the CPU RS LONG forms -- extended (AM=0) and indexed (AM=1) -- whose
* first halfword is now encoded from the auto-extracted GPC descriptors via the
* simulator's sentinel scheme (instrdefs.rs_hw1: d=0x3c extended / 0x3d indexed
* for SRS-type ops, a=0/1 for RS-type '/X' ops).  Covers SRS-type ops (A/L/ST/..,
* large displacement forces extended; explicit index forces indexed) and RS-type
* ops (AST/BAL/..).  Bases are 0-3 (base 4-7 promotes to the index register and
* stays on the legacy 3-bit-base path).  Verified byte-identical + matched against
* real BFS COMPILED object code.
RSLONG   CSECT
* --- extended (AM=0): large displacement, base 0-3 ---
         A     3,5000(2)
         AH    3,5000(2)
         L     7,1394(1)
         LH    5,500(2)
         ST    3,500(2)
         STH   3,500(2)
         S     3,5000(2)
         N     3,500(2)
         X     3,500(2)
         O     3,500(2)
         AE    2,500(2)
         LE    2,500(2)
         AST   3,500(2)
         NST   3,500(2)
         BAL   7,500(2)
* --- indexed (AM=1): explicit index register ---
         A     3,8(4,2)
         L     7,12(5,1)
         ST    3,16(6,2)
         LH    5,20(7,2)
         AE    2,24(4,1)
         LE    2,28(5,2)
         AST   3,8(4,2)
         BAL   7,8(4,2)
         END
