T        CSECT
* BVCF ("Branch on oVerflow/Carry, Forward") is a real instruction defined
* separately in the POO and used DIRECTLY in BFS source -- BFS.SRC as received/
* BFS_OI_BUILD.ASC line 2106:  `BVCF 6,*+39` -> DE99.  It encodes as an SRS short
* form 11011 mmm dddddd 01 (the 01 form bits distinguish it from BCF=00, BCB=10,
* BCTB=11).  The legacy direct-mnemonic path fed generateSRS form bits 11 (BCTB's),
* mis-encoding BVCF as DE9B; the descriptor path emits the flight-correct DE99.
* Here C is at halfword 0 and FWD at halfword 39, so the PC-relative displacement
* is 39 - (0+1) = 38, and BVCF 6,FWD -> 11011 110 100110 01 = DE99, reproducing
* the BFS object code exactly.
C        BVCF  6,FWD
         DS    38H
FWD      DS    0H
         END
