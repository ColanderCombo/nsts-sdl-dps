* ESD ordering: the AP101S assembler emits ESD entries in source-appearance
* order with SD (control sections) and ER (external refs) INTERLEAVED -- an
* EXTRN referenced before a later CSECT takes the lower ESD ID.  LD (ENTRY)
* definitions are not interleaved; they take trailing IDs and reference their
* owning section.  Verified vs real AP101S 3.0 listings (ASMLIB-BOS).  Source
* order here is SECTA, XFIRST, EHERE(entry), SECTB, YSECOND.
SECTA    CSECT
         EXTRN XFIRST
         ENTRY EHERE
EHERE    DC    A(XFIRST)
SECTB    CSECT
         EXTRN YSECOND
         DC    A(YSECOND)
         END
