* Forward =Y(label) literal whose value changes pass-to-pass.  The
* forward `B LATE` condenses RS->SRS between passes, shifting LATE, so
* =Y(LATE) evaluates to a different halfword each pass.  The literal pool
* used to dedup/compare entries by WHOLE-DICT equality (incl. value), so
* the shifting value tripped "Literal not in literal pool" / "Literal has
* changed value" every pass and the deck never converged.  literalIndex()
* now keys on the operand text and refreshes the slot's value/assembled,
* so the same =Y(LATE) is one stable pool slot and the deck converges.
* Both LH's share that slot; LATE settles at halfword 4.
T        CSECT
         BALR  R1,0
         USING *,R1
         B     LATE
         LH    R2,=Y(LATE)
LATE     LH    R3,=Y(LATE)
         LTORG
R1       EQU   1
R2       EQU   2
R3       EQU   3
         END
