* The @/# indirect form over a NUMERIC (absolute) displacement keeps
* the bare paren register as the BASE alone -- it must not be
* duplicated into the second halfword's X2 field: flight FCMTRACE
* +0019 'ST@# R4,0(R2)' encodes 34F6 1800 (AM=1, b=R2, x2=0,
* ia=i=1); the bare-paren index swap copied R2 into X2 as well,
* emitting 34F6 5800.  (A RELOCATABLE displacement still converts
* the register to an index -- see feat_paren_index_reloc.)
T6       CSECT
R2       EQU   2
R4       EQU   4
         ST@#  R4,0(R2)
         BR    R4
         END
