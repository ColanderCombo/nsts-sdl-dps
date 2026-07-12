*/ Minimal halUCP smoke test: print one SP scalar, then halt via SVC 0x15.

#EFPSMK  CSECT
         DC    X'0000'
         DC    X'0000'
         DC    A(#DFPSMK)
         DC    X'0000'
         DC    X'0005'

#DFPSMK  CSECT
PI       DC    X'413243F6'         3.14159 in IBM SP
SVCHLT   DC    X'0015'             SVC code 0x15 = halt (read by SVC)

$0FPSMK  CSECT
R0       EQU   0
R1       EQU   1
R3       EQU   3
R5       EQU   5
R6       EQU   6
F0       EQU   0

FPSMK    DS    0H
         ENTRY FPSMK

         EXTRN @0FPSMK
         EXTRN IOINIT
         EXTRN EOUT

*  Prolog: R0 = stack ER, R1 = data CSECT, R3 = data+4
         LHI   R0,@0FPSMK
         LHI   R1,#DFPSMK
         STH   R1,5(R0)
         IAL   R0,20
         LA    R3,4(R1)
         STH   R3,9(R0)

*  IOINIT(R5=mode 2 WRITE, R6=channel 6)
         LFXI  R6,6
         LFXI  R5,2
         SCAL  R0,IOINIT(R3)

*  EOUT(F0=PI)
         LE    F0,0(R1)            PI is at #DFPSMK+0
         SCAL  R0,EOUT(R3)

*  Halt: SVC reads code 0x15 from #DFPSMK+2 (after PI's 4 bytes)
         SVC   2(R1)

         END   FPSMK
