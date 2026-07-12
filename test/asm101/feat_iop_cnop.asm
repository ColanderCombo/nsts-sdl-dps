* The IOP @CNOP/#CNOP align the (shared) location counter exactly like the CPU
* CNOP pseudo-op: `CNOP n` rounds the counter up to an n-halfword boundary
* (n=2 -> fullword).  A label on the line takes the aligned address.  These were
* previously dispatched to the IOP machine encoder, which has no descriptor for
* them (they are pseudo-ops, not instructions) -- a latent crash now routed here.
* @LI and @SIO are one halfword each, so the counter is odd ahead of each @CNOP.
ICNOP    CSECT
         @LI   0                  hw0 -> counter at hw1 (odd)
ACN      @CNOP 2                  align hw1 -> hw2; ACN = 2
         @SIO  0                  hw2 -> counter at hw3 (odd)
BCN      #CNOP 2                  BCE form, same alignment: hw3 -> hw4; BCN = 4
         @LI   0
         END
