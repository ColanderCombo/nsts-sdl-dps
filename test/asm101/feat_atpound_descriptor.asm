* @/# RS addressing variants now route through the BASE op's descriptor.  The @
* (indirect) and # (indexed-immediate) suffixes select only the RS AM=1 ia/i mode
* bits in the second halfword; they share the base op's opcode, so the first
* halfword comes from the base descriptor (has_rs_descriptor / rs_hw1 strip the
* suffix via instrdefs.base_op).  Covers descriptor-bearing bases A/L/ST/N (@,
* indirect) and LH/STH (@#, indexed) -- byte-identical to the legacy duplicate-
* opcode keys (golden + dual-path force-legacy check).  BO@/BO#/BO@#, whose base
* BO has no descriptor (and is table-coded with the BC placeholder, not BO's
* overflow opcode), stay on the legacy path and are unaffected.
ATPND    CSECT
R0       EQU   0
R1       EQU   1
R3       EQU   3
         USING ATPND,R0
PTR      DC    X'00000000'
         A@    R1,PTR
         L@    R1,PTR
         ST@   R1,PTR
         N@    R1,PTR
         LH@#  R1,PTR(R3)
         STH@# R1,PTR(R3)
         END
