* USING whose base symbol is a forward reference into a later CSECT, with
* an UNLABELED statement ahead of the base so the pre-pass 'preliminary'
* estimate (4 bytes per LABELED statement) disagrees with the settled
* layout.  optimizeScratch must re-resolve the base (refreshUsing) or the
* displacement computes negative and 'A R6,AA' wrongly stays RS (4 bytes)
* where the flight assembler condenses to SRS -- the RUNASM SQRT
* 'USING A,R1' / 'A R6,A' case.
TEST     CSECT
R1       EQU   1
R6       EQU   6
R7       EQU   7
         USING AA,R1
         A     R6,AA
         A     R7,BB
DATA     CSECT
PFX      DC    H'1'
         DC    H'2'
AA       DC    F'3'
BB       DC    F'4'
         END
