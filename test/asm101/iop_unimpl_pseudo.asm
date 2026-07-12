* A BCE-looking name that is really an MLIB80 library macro -- here #ORG, assembled
* WITHOUT the macro library -- is NOT an instruction.  It must read as a plain
* "Unrecognized line" (the same diagnostic as any unknown operation), and must not
* crash with a None-descriptor traceback or be treated as a known IOP op.  The
* native alignment forms @CNOP/#CNOP are real ops, handled separately.
T        CSECT
         #ORG  0
         END
