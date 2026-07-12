* Exercises SRS SHORT-form encoding now driven from the GPC descriptors:
* generateSRS encodes only the fields the descriptor declares, which covers the
* shapes that carry their discriminator in FIXED bits and were previously shed to
* the legacy bit-twiddle -- shifts (x/d layout, shift type fixed) and the no-R1
* ops TD/TH/ZH/SHW (d/b layout, implied R1 fixed).  Small displacements keep
* every instruction a 2-byte SRS short form.  Byte-identical to the legacy
* packing (golden + exhaustive dual-path check).  The SRS branch short forms are
* covered elsewhere via their real synthesis paths: BCF/BCB/BCTB by feat_bcf_using
* and feat_bctb_branch (from BC/BCT), BVCF by feat_ovfl_carry_branch (BOV/BOC/BVC).
SRSSHORT CSECT
         SLL   3,4
         SLDL  4,2
         SRA   3,2
         SRDA  4,6
         SRDL  5,1
         SRL   6,3
         SRR   7,5
         SRDR  2,7
         TD    4(1)
         TH    8(3)
         ZH    12(2)
         SHW   16(0)
         END
