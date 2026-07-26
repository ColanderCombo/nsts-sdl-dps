* Literal-pool mechanics, three flight-traced fixes:
* (a) a mid-csect LTORG occupies its pool's size, so following statements
*     start past it -- IBM FIOLGERR: pool at hw 0x86, the trailing DC Z
*     ZCONs at 0x88/0x8A, csect 140 hw; ours overlapped them onto the
*     pool at 0x86 and sized the csect 136;
* (b) the end-of-source pool ORIGIN is FULLWORD-aligned, not aligned to
*     its widest literal -- IBM FPMRES =Y(FPMXQELE) at hw 0x44 and
*     FPMWAIT =H'1' at hw 0x56, each with one gap halfword after code;
* (c) a =Y literal over a relocatable value carries a pool-slot Y RLD --
*     FPMREL =Y(FPMXQELE) is 0000 + RLD (links 8B86); FIOERRLC
*     =Y(FIOADBST+8) keeps the addend 0008 + RLD against the EXTRN
*     (ours emitted the bare numeric image with no RLD).
LP       CSECT
R1       EQU   1
R5       EQU   5
         EXTRN EXTV
         N     R5,=X'000007FF'
         LTORG
ZC1      DC    Z(,EXTV,0)
         CH    R1,=Y(LOCLBL)
         CH    R1,=Y(EXTV+8)
         BR    R5
LOCLBL   DS    0H
         END
