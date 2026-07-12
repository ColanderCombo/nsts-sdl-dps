* Exercises the CPU RR instruction family, which is encoded from the
* auto-extracted GPC descriptors (instrdefs.CPU) -- the Stage-3a slice.  Covers
* every RR mnemonic whose descriptor encoding was verified equal to the legacy
* model101tables packing, so the byte-golden test guards the whole descriptor
* path (no other fixture used an RR op).  Operands are 3-bit register numbers;
* MR/DR use an even R1; SPM takes a single register.
RRTEST   CSECT
         AR    3,4
         SR    5,6
         MR    2,4
         DR    2,6
         NR    3,4
         OR    5,6
         XR    7,1
         LR    3,4
         CR    5,6
         LCR   7,2
* LACR: an alias of LCR (PFS MAFGEN disassembly renders LACR back as LCR), so it
* encodes through LCR's descriptor via instrdefs.CPU_ALIASES.
         LACR  7,2
         ICR   3,4
         NCT   3,4
         SUM   3,4
         XUL   3,4
         MVH   3,4
         CBL   3,4
         BALR  7,4
         BCTR  3,5
         BCR   7,4
         BCRE  7,4
         BVCR  3,4
* PC: descriptor-encoded (11011xxx11101yyy); the legacy argsRR value was a
* mis-encoding -- BFS.SRC/COMPILED/STARTUP shows PC G4,G4 -> DCEC, matched here.
         PC    4,4
         SPM   3
         SRET  3,4
         LFLR  3,4
         LFXR  3,4
         LXAR  3,4
* STXAR: enriched into cpu_instr.coffee 2026-06-22 (descriptor 10100xxx11101yyy),
* byte-identical to the legacy packing -- left RR_LEGACY_OPCODES for the descriptor.
         STXAR 3,4
         LFLI  3,4
         LFXI  3,4
         CVFL  2,4
         CVFX  2,4
         LER   2,4
         AER   2,4
         SER   2,4
         MER   2,4
         DER   2,6
         CER   2,4
         LECR  3,4
         AEDR  2,4
         SEDR  2,4
         MEDR  2,4
         DEDR  2,6
         CEDR  2,4
         END
