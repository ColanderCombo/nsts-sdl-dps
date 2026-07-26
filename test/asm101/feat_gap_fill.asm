* CPU-csect alignment gaps are covered by TXT and filled with C9FB --
* the SVC opcode halfword, a stray-execution trap -- not zeros.  IBM
* prints the fill in the RUNLST listings: STBYTE '0000D C9FB' before a
* 'DS 0F'; CASV +001B / IREM +000F / CPASP +00011 before an LTORG;
* ITOC +00065 before a 'DC F'; the flight image holds C9FB at every
* asm-csect alignment gap, including the end-of-source pre-pool gap
* (DASS FPMRES +0043 'C9FB *** ALIGNMENT GAP ***' -- pinned in
* feat_ltorg_pool's golden).  CNOP gaps keep their own executable-NOP
* fill (D800; C000 for @CNOP/#CNOP).
TG       CSECT
R2       EQU   2
         LH    R2,=H'1'
         DC    H'5'
         LTORG
         DC    H'7'
         DC    H'11'
         DC    F'9'
         DC    H'13'
         CNOP  2
         BR    R2
         END
