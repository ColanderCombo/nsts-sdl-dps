* R3 COD-ED as a base register (explicit paren) carries the ABSOLUTE
* displacement, not a PC-relativized one: flight FCMISYNC encodes
* 'L R4,TPSAE2OP-TPSASTRT(R3)' as 1CF3 0088 (AM=0, d=value) where the
* b2==3-as-PC-sentinel conflation used to emit 1CF7 <pc-rel> -- correct
* EA only by the accident R3=0.  The displacement expression is symbolic
* (EQU difference), past SRS range, mirroring the TPSA field reference.
T3       CSECT
R3       EQU   3
R4       EQU   4
ABASE    EQU   0
AFIELD   EQU   136
         L     R4,AFIELD-ABASE(R3)
* LA over an explicit R3 base likewise: 'LA R1,TPSAPC1-TPSASTRT(R3)' is
* E9F3 00B0 in flight (DASS_SSW FPMIHPC2+157) -- the LA based-form rule
* must not except b2==3 (selfRelB2 alone marks the PC sentinel).
R1       EQU   1
BFIELD   EQU   176
         LA    R1,BFIELD-ABASE(R3)
         END
