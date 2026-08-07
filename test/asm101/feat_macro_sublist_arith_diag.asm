* A whole sublist used as an arithmetic term is a program error to
* diagnose, not coerce (HLASM SC26-4940 Table 58: the value "must be a
* self-defining term").  ASM101S crashed here with a Python TypeError
* ('<=' between str and int) -- the opening traceback of
* virtualagc/virtualagc#1331; asm101 must instead report a clean
* intolerable diagnostic and exit nonzero.
         MACRO
         CRASH
         AIF     (&SYSLIST(1) LE 0 OR &SYSLIST(1) GE 07).INV
         MNOTE   'CRASH: VALID'
         MEXIT
.INV     MNOTE   'CRASH: INVALID'
         MEND
T        CSECT
         CRASH   (CH,R4,GE,TPCTPRI)
         END
