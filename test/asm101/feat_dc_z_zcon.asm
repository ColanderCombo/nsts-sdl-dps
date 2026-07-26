* DC Z(...) / =Z(...) ZCON relocation subfields.  The relocated address
* is the CODE subfield in 'Z(sym...)' -> ZCON/code RLD (flag 0x04), but
* the DATA subfield in 'Z(,sym...)' (code subfield EMPTY) -> ZCON/data
* RLD (flag 0x50), so the linker writes HW0 AND patches the DSR field:
* flight FCMNINIT +01DB '=Z(,FPMXQETB+2,0)' links 8B6C 0001 (target
* 0x18B6C, sector 1 -> DSR=1); punching 0x04 left HW1's DSR unpatched
* (0000).  The ZCON flags byte sits in the data image at byte 2 either
* way.
ZCONT    CSECT
R3       EQU   3
         EXTRN EXTSYM
ZREF     DC    Z(,EXTSYM,X'8')
ZCODE    DC    Z(EXTSYM,,X'8')
         L     R3,=Z(,EXTSYM+2,0)
         DS    F
         END
