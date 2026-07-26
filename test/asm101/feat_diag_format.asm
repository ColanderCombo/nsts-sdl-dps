DIAGT    CSECT
         MACRO
         GENBAD &SYM
         DC    Y(&SYM)
         MEND
         GENBAD NOSUCH
         END
