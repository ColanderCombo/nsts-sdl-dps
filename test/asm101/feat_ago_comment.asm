* AGO target must ignore a trailing comment on the operand.
* "AGO .LOOP   KEEP GOING" was being used verbatim as the target, so the
* comment glued onto the sequence symbol never matched ".LOOP" and the
* backward loop-jump degraded into a forward skip that found nothing --
* the conditional-assembly counting loop then ran exactly once.
* This mirrors MACSMITH's TABLGEN/MSGLINES ("AGO .GEN   LOOP BACK").
         MACRO
&LAB     GENVALS
         LCLA  &A
&A       SETA  1
.LOOP    ANOP
         DC    H'0'                 ONE HALFWORD PER ITERATION
         AIF   (&A EQ 4).DONE       STOP AFTER 4 -> .DONE
&A       SETA  &A+1                 BUMP COUNTER
         AGO   .LOOP                KEEP GOING (trailing comment)
.DONE    ANOP
&LAB     EQU   *                    END LABEL AFTER 4 HALFWORDS
         MEND
T        CSECT
TOP      GENVALS                    EMIT 4 HALFWORDS, THEN TOP+8 LABEL
         END
