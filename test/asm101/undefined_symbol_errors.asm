A        CSECT
* A genuinely undefined symbol (never defined in any pass) must STILL be
* an intolerable error in the final pass -- the final-pass counting rule
* must not suppress real errors, only transient forward references.
BAD      DC    Y(NEVERDEFINED)
         END
