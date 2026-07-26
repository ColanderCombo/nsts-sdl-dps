* sects[].used must not RATCHET across compile passes: only pos1 was
* reset between passes, so a pass that converged SMALLER kept the
* previous pass's high-water mark, sized the csect one halfword long,
* and padded a phantom trailing 0000 into the TXT -- flight FIOPDHF is
* 294 hw (DASS), ours sized 295 (+0126,+1).  Repro: the first forward
* branch is exactly at the SRS boundary (56 hw) until the second one
* condenses, so pass N lays it RS (56 hw total) and pass N+1 condenses
* it to SRS (55 hw) -- the stale 'used' kept 56.
TC       CSECT
R2       EQU   2
         B     TGT
         DC    52H'0'
         B     TGT
TGT      DS    0H
         END
