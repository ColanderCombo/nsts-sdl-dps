* A multi-suboperand DC must emit each suboperand ONCE.  The DC buffer
* was reset per STATEMENT while every type handler emits
* dcBuffer[:dcBufferPtr], so 'DC Y(X),H'130'' laid down Y,Y,H (FCMCBLKS
* FCMHTABL: 6 spurious halfwords, +6 hw csect size vs the IBM listing's
* 00010082) and a Z/Y/A adcon after another suboperand also carried a
* skewed RLD position.
TESTDC   CSECT
V        EQU   2
TAB      DC    Y(V-1),H'130'
         DC    A(V),H'7'
LOC      DC    Y(0)
ADC      DC    H'1',Y(LOC)
         END
