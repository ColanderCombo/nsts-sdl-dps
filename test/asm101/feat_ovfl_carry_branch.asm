T        CSECT
* Overflow/carry branches.  BOV (overflow, mask 1), BOC (carry, mask 2) are
* implied-mask extended mnemonics; BVC M1,D2 is the explicit-mask form.  All
* encode in the BVCF family (SRS forward, bb=01).  Each target is 3 halfwords
* ahead -> displacement 2, matching the OI301700 ground-truth listing:
*   BOV -> D909 (mask 1), BOC -> DA09 (mask 2), BVC 6 -> DE09 (mask 6).
* The condition-code BO/BNO are a DIFFERENT family (BCF, bb=00) and must be
* left unchanged: BO -> D908, BNO -> DE08.
BR1      BOV   T1
         DC    H'0'
         DC    H'0'
T1       BOC   T2
         DC    H'0'
         DC    H'0'
T2       BVC   6,T3
         DC    H'0'
         DC    H'0'
T3       BO    T4
         DC    H'0'
         DC    H'0'
T4       BNO   T5
         DC    H'0'
         DC    H'0'
T5       DS    0H
         END
