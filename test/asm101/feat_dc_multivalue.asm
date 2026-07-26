* A comma-separated nominal-value list inside ONE F/H constant emits one
* field PER value (like E/D already did).  Flight DCI#DATA's CLOCMSGL is
* `DC X'18',X'48',H'0,1,160,37,0,0,0'`; emitting only the first value left
* #DDCICYC 6 hw short (670 vs 676) and cascaded the whole bank-0 tail -6.
TESTMV   CSECT
         DC    H'0,1,160,37,0,0,0'   7 halfwords
HAFTER   DS    0H                    -> halfword offset 7
         DC    F'1,2,3'              3 fullwords (fullword-aligned)
FAFTER   DS    0H                    -> halfword offset 7+1(pad)+6 = 14
         END
