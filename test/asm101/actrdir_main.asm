* ACTR n directive: sets the conditional-assembly loop counter (the AIF/
* AGO branch budget) for this expansion.  ACTRDIR takes 5000 AIF
* branches -- more than the 4096 default -- so it completes (and emits
* DC H'1' = 0001) only if `ACTR 8000` raised the budget.  Before the fix
* ACTR was unhandled: it reached codegen as "Unrecognized line" AND the
* loop hit the 4096 default cap before finishing (MLIB80 ENDCASE relies
* on ACTR 30000 for the same reason).
T        CSECT
         ACTRDIR
         END
