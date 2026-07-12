T        CSECT
* EXTRN symbol used with a constant offset as an instruction operand
* (e.g. `LH R4,FAZ2STRT+3`).  The bare-EXTRN case resolves an exact
* hashcode; with an offset, the evaluated displacement is hashcode+offset,
* which unhash() decodes back to the EXTRN name plus the offset.  Codegen
* emits the offset as the RS displacement and leaves the symbol itself to
* the linker via a Y-type RLD.  Ground truth (OI301700 GPCERAS):
*   LH R4,FAZ2STRT+3 -> 9CF3 0003   (displacement holds the +3 offset)
* Before the fix this produced "Could not interpret operand" and emitted
* no object code, drifting every following label's address.
R4       EQU   4
R5       EQU   5
         EXTRN FAZ2STRT
L0       LH    R4,FAZ2STRT+3
L1       LH    R5,FAZ2STRT+1
L2       LH    R4,FAZ2STRT
         END
