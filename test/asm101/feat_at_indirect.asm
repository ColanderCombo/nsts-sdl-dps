* Bare-operand indirect (@) / indexed (#) addressing.  These suffixes
* live only in the RS AM=1 encoding (generateRS1 carries the ia/i bits;
* AM=0 has no such fields), so the mnemonic must encode AM=1 even when a
* low base register (here R0 via USING, opcode odd) sets forceAM0.  The
* bare form `LDM@# R1,PTR` (no explicit index/base register) used to fall
* through every AM=1/AM=0 branch to "Could not interpret line as SRS or
* RS" and emit zero bytes; the indexed form `LDM@# R1,PTR(R3)` dodged
* forceAM0 via its explicit X2 (forceAM1) and already worked.
ATIND    CSECT
R0       EQU   0
R1       EQU   1
R3       EQU   3
         USING ATIND,R0
PTR      DC    X'00000000'
L0       LDM@# R1,PTR
L1       LXA@  R1,PTR
L2       LDM@# R1,PTR(R3)
         END
