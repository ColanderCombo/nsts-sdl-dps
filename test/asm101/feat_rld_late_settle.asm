* RLDs must describe the final pass, not pass 3.
*
* The compile fixpoint runs pass 3, 4, 5, ... until the symbol table and the
* csect layout stop moving, rewriting the memory image from scratch every
* time.  RLDs used to be emitted on pass 3 alone and accumulated across the
* rest, so any value that settled after pass 3 got a correct text word and a
* relocation entry describing where it USED to point.
*
* An EQU chain resolves exactly one link per pass -- 'E1 EQU E2' is processed
* before E2 is redefined, so it takes E2's value from the previous pass -- so
* the chain below needs seven passes.  At pass 3 'E1' is still short of TGT
* and lands inside S1; only at pass 7 does it reach S2.  The emitted halfword
* is right either way (the image is rewritten each pass); it is the
* relocation ESDID that was frozen wrong, which is invisible until the linker
* places S2 somewhere other than immediately after S1.
*
S1       CSECT
S1TOP    DS    0H
         DC    Y(E1)         late-settling: must relocate against S2
         DC    Y(S1TOP)      control: stable from pass 3, relocates to S1
         DC    Y(0)          keeps S1TOP strictly inside S1's extent
E1       EQU   E2
E2       EQU   E3
E3       EQU   E4
E4       EQU   TGT
S2       CSECT
         DC    Y(2)
TGT      DS    0H
         DC    Y(3)
         END
