* Phase 1 relocatability classification (IEUF8V-style), read from the section
* hash via classify()/unhash.  Pins BOTH directions:
*   SIMPLE   Y(A)    -> one CSECT term, silent, emits an RLD
*   ABSOLUTE Y(B-A)  -> same-section difference cancels to a number, silent,
*                       NO RLD (regression-guards paired-term cancellation)
*   COMPLEX  Y(A*2)  -> relocatable * constant, flagged (not single-RLD-able)
*   COMPLEX  Y(A+B)  -> sum of two relocatable terms, flagged
* The two COMPLEX lines are intolerable (assembly aborts at default tolerance);
* at --tolerable 255 they are reported but the module still writes, leaving
* exactly one RLD (for the SIMPLE Y(A)).
T        CSECT
A        DS    F
B        DS    F
SIMP     DC    Y(A)
ABSL     DC    Y(B-A)
CPLX1    DC    Y(A*2)
CPLX2    DC    Y(A+B)
         END
