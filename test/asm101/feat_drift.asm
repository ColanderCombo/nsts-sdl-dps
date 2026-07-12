* Address-drift / SRS-RS branch-condense fixpoint regression.
* TOP branches forward to FAR.  At all-4-byte sizing the 30 BE
* branches between them span >=56 halfwords, so optimizeScratch
* sizes TOP as RS; once those 30 condense to SRS the span is <56,
* so the compile pass condenses TOP to SRS too.  That shrinks FAR
* by one halfword versus the pass-2 layout.  Without the compile
* fixpoint, TOP keeps the over-estimated displacement (DC7C, reach
* FAR+1) and silently overshoots; with it, TOP re-resolves against
* FAR's settled address (DC78, disp 30) and CHK's backward branch
* also lands exactly on FAR.
DRIFT    CSECT
TOP      BE    FAR
B00      BE    B01
B01      BE    B02
B02      BE    B03
B03      BE    B04
B04      BE    B05
B05      BE    B06
B06      BE    B07
B07      BE    B08
B08      BE    B09
B09      BE    B10
B10      BE    B11
B11      BE    B12
B12      BE    B13
B13      BE    B14
B14      BE    B15
B15      BE    B16
B16      BE    B17
B17      BE    B18
B18      BE    B19
B19      BE    B20
B20      BE    B21
B21      BE    B22
B22      BE    B23
B23      BE    B24
B24      BE    B25
B25      BE    B26
B26      BE    B27
B27      BE    B28
B28      BE    B29
B29      BE    FAR
FAR      DC    H'1'
CHK      BE    FAR
         END
