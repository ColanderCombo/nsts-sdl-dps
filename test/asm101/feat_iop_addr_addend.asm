* Relocatable addends on the 18-bit IOP (MSC/BCE) long-format 'a' field.
IOPADDEN CSECT
         ENTRY IOPADDEN
         EXTRN FA,FB
IOPNEG   #LBR@ FA-2
IOPNEGM  #MOUT@ FB-2
IOPPOS   #LBR@ FA+2
IOPPOSM  #MOUT@ FB+20
IOPZERO  #BU   FA
         END
