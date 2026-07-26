* The COMMA form 'expr(,Rn)' on an @/# op names the BASE with the index
* slot explicitly empty -- the atStar bare-paren heuristic (paren reg is
* an index) must not move it into X2: flight FPMDISP 'STDM@
* 0,TPSAWORK+1-TPSASTRT(,R3)' encodes 90FF 10B3 (x2=0, b=R3), ours put
* the base in X2 (70B3).
TESTAT   CSECT
R3       EQU   3
WRK      EQU   179
         STDM@ 0,WRK(,R3)
         LPS@  WRK-1(,R3)
         END
