* 'BALR Rn,0' / 'USING *,Rn' -- the assembler-established base idiom.  The
* USING names no symbol, so its base is a raw location counter value, and
* the collect pass's location counter is an OVER-ESTIMATE wherever an
* ambiguous SRS/RS statement ahead of the USING has not yet condensed (the
* collect pass sizes those long).  optimizeScratch must re-resolve the '*'
* against the settled layout the same way it re-resolves a forward-
* referenced base symbol; if it does not, the base reads high by the whole
* accumulated over-estimate, every displacement under it computes negative,
* and NOTHING over that base condenses -- each reference stays an RS long
* form one halfword too big.
*
* Here four forward branches ahead of the BALR each condense RS->SRS, so
* the collect pass puts '*' four halfwords past its settled value.  'ST
* R4,WORD1' sits two halfwords past the base: with the settled base it is
* SRS 3404 (D2=1, counted in FULLWORDS for a fullword-addrWidth op, over
* base register 0); with the stale base its displacement is -2 and it
* wrongly stays RS 34F0 0002.
TSTAR    CSECT
R0       EQU   0
R4       EQU   4
R6       EQU   6
         DC    H'0'                PARK THE BALR ON AN ODD HALFWORD
         B     L1
L1       B     L2
L2       B     L3
L3       B     L4
L4       BALR  R0,0
         USING *,R0
         B     OVER
WORD1    DC    F'0'
WORD2    DC    F'0'
OVER     ST    R4,WORD1
         L     R6,WORD2
         END
