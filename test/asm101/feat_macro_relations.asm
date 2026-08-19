* Macro-language relational semantics + keyword/continued arguments,
* from virtualagc/virtualagc#1331 and its z390/HLASM verification:
*   - character relations collate in EBCDIC ('A'=C1 < '1'=F1), and a
*     shorter string is always less (HLASM SC26-4940 PDF p.388);
*   - a null parameter or &SYSLIST element used arithmetically is 0
*     (Assembler H GC26-3758-3 p.19), so "AIF (&SYSLIST(1) LE 0 ...)" 
*     branches on an omitted operand;
*   - keyword arguments (ACALL=YES, TITLE='...') must bind, and a
*     continued invocation must join before operand scan (both were
*     dropped by ASM101S).
         MACRO
         CMPTEST
         AIF     ('A' LT '1').K1
         MNOTE   'FAIL: A LT 1 (EBCDIC A=C1 before 1=F1)'
         AGO     .N1
.K1      MNOTE   'OK: A LT 1'
.N1      AIF     ('B' LT 'AB').K2
         MNOTE   'FAIL: B LT AB (shorter collates less)'
         AGO     .N2
.K2      MNOTE   'OK: B LT AB'
.N2      AIF     ('AB' LT 'B').K3
         MNOTE   'OK: AB NOT LT B'
         AGO     .N3
.K3      MNOTE   'FAIL: AB LT B (longer cannot be less)'
.N3      ANOP
         MEND
         MACRO
         CCTEST
         AIF     (&SYSLIST(1) LE 0 OR &SYSLIST(1) GE 07).INV
         MNOTE   'CC VALID: >&SYSLIST(1)<'
         MEXIT
.INV     MNOTE   'CC INVALID: >&SYSLIST(1)<'
         MEND
         MACRO
&N       AMAIN   &ACALL=NO,&TITLE=
         MNOTE   'AMAIN NAME=&N ACALL=&ACALL TITLE=&TITLE'
         MEND
         MACRO
         IFPROC  &CC,&P1,&P2,&P3,&P4,&P5,&P6,&P7,&P8,&P9,&P10
         LCLA    &NS
&NS      SETA    N'&SYSLIST
         MNOTE   'IFPROC P1=>&P1< P7=>&P7< P9=>&P9< NS=&NS'
         MEND
T        CSECT
         CMPTEST
         CCTEST
         CCTEST  3
         CCTEST  8
ACOS     AMAIN   ACALL=YES
         AMAIN   TITLE='TEST ROUTINE'
         IFPROC  ,(CH,R4,GE,TPCTPRI),,,,,,                             X
               ZZ,,LAST
         END
