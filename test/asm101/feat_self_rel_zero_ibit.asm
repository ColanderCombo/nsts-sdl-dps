* A self-relative RS target at ZERO displacement takes the SUBTRACTIVE
* I-bit form like backward ones: 'BAL R3,*+2' is E3F7 0800 in the IBM
* listings (i=1, d=0, EA = IC - 0; POO g_EA step 4), never the additive
* E3F7 0000 (FPMRES+30; 3/3 OI30 instances).
* ...but a real R3 BASE with an index and zero displacement must STAY
* additive: 'STH R4,0(R5,3)' is BCF7 A000 in RUNLST ITOC/KTOC (i=0),
* not A800 -- the subtractive fold above must gate on a genuinely
* self-relative target (selfRelB2), not on ib2 == 3 (base == R3), which
* an indexed store also presents.
TESTSR   CSECT
R3       EQU   3
R4       EQU   4
R5       EQU   5
         BAL   R3,*+2
         DC    H'0'
IDX      STH   R4,0(R5,3)
         END
