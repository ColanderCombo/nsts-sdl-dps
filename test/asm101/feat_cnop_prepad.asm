* CNOP's alignment pad is the CNOP's OWN object code, so the statement's
* location -- and therefore any label on the line -- is the location counter
* BEFORE the pad, not the aligned address after it.  (A DC/DS names its
* POST-alignment start instead: that gap belongs to nobody.)  The next
* statement sits at the CNOP's own location plus the pad, and a labelled CNOP
* takes that pre-pad value -- such a label can be a branch target, so this is
* object code, not listing cosmetics.
*
* Pad value is typed by context: CPU CNOP pads with BCF 0,0 = D800, the IOP
* @CNOP/#CNOP with the DLYI-0 no-op = C000 (a D800 there would be a CPU opcode
* in IOP instruction space).  CNOP 2 aligns to a fullword (even halfword),
* CNOP 1 to an ODD halfword; an already-aligned counter emits no pad at all,
* and then the pre-pad and aligned locations coincide.
CNPRE    CSECT
         DC    H'0'         hw 0; counter -> hw 1 (odd)
PADCPU   CNOP  2            PADCPU = 1, D800 pad at hw 1, counter -> hw 2
NOPADC   CNOP  2            already fullword: NOPADC = 2, no pad emitted
         @LI   0            hw 2; counter -> hw 3 (odd)
PADIOP   @CNOP 2            PADIOP = 3, C000 pad at hw 3, counter -> hw 4
         @LI   0            hw 4; counter -> hw 5 (odd)
ODDOK    CNOP  1            already odd: ODDOK = 5, no pad emitted
         DC    H'0'         hw 5; counter -> hw 6 (even)
PADODD   CNOP  1            PADODD = 6, D800 pad at hw 6, counter -> hw 7
AFTER    DC    H'0'         AFTER = 7
         END
