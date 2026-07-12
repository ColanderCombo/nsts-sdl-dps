*/ AP-101S floating-point test program.
*/ Exercises arithmetic, compare, conversion, and exception-path
*/ instructions, printing each result via halUCP EOUT.
*/
*/ This version installs a real exception handler at PSW vector 0x4C
*/ (per POO §2.5.2 program-check vector).  The handler captures the
*/ intCode of each exception into EXCBUF, then resumes the interrupted
*/ instruction stream via LPS of the saved old PSW at 0x48.
*/
*/ Layout follows the conventions a HAL/S PROGRAM emits:
*/   #EFPTEST  - entry header (linker fixup)
*/   #DFPTEST  - data CSECT (constants + EXCBUF)
*/   $0FPTEST  - main code CSECT
*/   @0FPTEST  - synthetic stack ER (linker --generate-stacks)

#EFPTEST CSECT
         DC    X'0000'
         DC    X'0000'
         DC    A(#DFPTEST)
         DC    X'0000'
         DC    X'0005'

#DFPTEST CSECT
PI       DC    X'413243F6'
TWO      DC    X'41200000'
HALF     DC    X'40800000'
ZERO     DC    X'00000000'
BIG      DC    X'7FFFFFFF'
SMALL    DC    X'00100000'
ANOMHI   DC    X'46000000'
ANOMLO   DC    X'45F00000'
SVCHLT   DC    X'0015'
EXCIDX   DC    X'0000'
EXCBUF   DC    X'0000'
         DC    X'0000'
         DC    X'0000'
         DC    X'0000'

$0FPTEST CSECT
R0       EQU   0
R1       EQU   1
R2       EQU   2
R3       EQU   3
R4       EQU   4
R5       EQU   5
R6       EQU   6
R7       EQU   7
F0       EQU   0
F1       EQU   1

FPTEST   DS    0H
         ENTRY FPTEST

         EXTRN @0FPTEST
         EXTRN IOINIT
         EXTRN EOUT
         EXTRN HOUT

*  Prolog (HAL/S PROGRAM convention)
         LHI   R0,@0FPTEST
         LHI   R1,#DFPTEST
         STH   R1,5(R0)
         IAL   R0,20
         LA    R3,4(R1)
         STH   R3,9(R0)

*  ============================================================
*  Install exception handler PSW at halfword 0x4C
*    PSW1 hi (halfword 0x4C) = NIA: low 15 bits of handler addr
*                                + bit 15 set (use BSR for high)
*    PSW1 lo (halfword 0x4D) = BSR=2 (code sector), DSR=0
*    PSW2  (halfwords 0x4E,0x4F) = 0  (no masks, no problem state)
*  ============================================================
         LHI   R7,EXNHDR           R7 hi = low 16 of handler addr
         AHI   R7,X'8000'          set bit 15 -> BSR-relative NIA
         STH   R7,X'4C'            PSW1 hi (NIA)
         LHI   R7,X'0020'          BSR=2, DSR=0
         STH   R7,X'4D'            PSW1 lo
*  PSW2: set bit 19 ('w' in DESC2) so handler isn't in wait state
         LHI   R7,X'0008'
         STH   R7,X'4E'            PSW2 hi (w-bit set, no masks)
         LHI   R7,0
         STH   R7,X'4F'            PSW2 lo

         LFXI  R6,6                channel 6
         LFXI  R5,2                mode WRITE

*  Test 1: print PI directly (sanity)
         SCAL  R0,IOINIT(R3)
         LE    F0,0(R1)
         SCAL  R0,EOUT(R3)

*  Test 2: AER  PI + 2.0
         SCAL  R0,IOINIT(R3)
         LE    F0,0(R1)
         LE    F1,2(R1)
         AER   F0,F1
         SCAL  R0,EOUT(R3)

*  Test 3: SER  PI - 2.0
         SCAL  R0,IOINIT(R3)
         LE    F0,0(R1)
         LE    F1,2(R1)
         SER   F0,F1
         SCAL  R0,EOUT(R3)

*  Test 4: MER  PI * 2.0
         SCAL  R0,IOINIT(R3)
         LE    F0,0(R1)
         LE    F1,2(R1)
         MER   F0,F1
         SCAL  R0,EOUT(R3)

*  Test 5: DER  PI / 2.0
         SCAL  R0,IOINIT(R3)
         LE    F0,0(R1)
         LE    F1,2(R1)
         DER   F0,F1
         SCAL  R0,EOUT(R3)

*  Test 6: DER PI / 0.0
         SCAL  R0,IOINIT(R3)
         LE    F0,0(R1)
         LE    F1,6(R1)
         DER   F0,F1
         SCAL  R0,EOUT(R3)

*  Test 7: AER BIG + BIG
         SCAL  R0,IOINIT(R3)
         LE    F0,8(R1)
         AER   F0,F0
         SCAL  R0,EOUT(R3)

*  Test 8: MER SMALL * SMALL
         SCAL  R0,IOINIT(R3)
         LE    F0,10(R1)
         MER   F0,F0
         SCAL  R0,EOUT(R3)

*  Print captured exception codes via HOUT
         SCAL  R0,IOINIT(R3)
         LH    R5,18(R1)
         SCAL  R0,HOUT(R3)
         SCAL  R0,IOINIT(R3)
         LH    R5,19(R1)
         SCAL  R0,HOUT(R3)

*  Halt
         SVC   16(R1)

*  ============================================================
*  Exception handler.  Reached via PSW swap on programCheck.
*  Saved old PSW lives at halfwords 0x48..0x4B; intCode is the low
*  16 bits of PSW2 (halfword 0x4B).
*  We use R7 as scratch (caller's R7 was already saved by swapPSW
*  *into the old PSW save area*; wait — PSW save only saves PSW1+PSW2,
*  NOT GPRs.  So we must preserve registers we touch — see notes).
*
*  For safety we use R4 and R7, and restore R1 by reloading it from
*  the saved PSW area is overkill.  Simpler: save R4/R7 in EXCSAVE,
*  do the work, restore, then LPS.
*  Actually the operands of DER/MER aren't in R4/R7, so we can clobber
*  those without breaking the resumed instruction.  But R1 holds our
*  data base — must be untouched (and it is — handler reads it).
*  ============================================================
EXNHDR   DS    0H
         LH    R7,X'4B'
         LH    R4,17(R1)
         STH   R7,18(R4,R1)
         AHI   R4,1
         STH   R4,17(R1)
         LPS   X'48'(R2)

         END   FPTEST
