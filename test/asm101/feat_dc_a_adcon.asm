* Regression: DC A(symbol) must evaluate the address constant AND emit a
* 4-byte (fullword) RLD for the relocatable term -- before the fix the A-type
* DC branch only handled the self-defining A'hex' form and silently emitted 4
* zero bytes with NO relocation (an A(EXTRN)/A(label) could never be linked).
* Also covers the dup-factor (DC nA'hex' previously infinite-looped).
ADCON    CSECT
         EXTRN EXTSYM
HERE     DC    A(THERE)
EREF     DC    A(EXTSYM)
HEX      DC    2A'1F'
THERE    DS    F
         END
