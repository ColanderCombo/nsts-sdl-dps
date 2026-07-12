T        CSECT
* A register field may be a PARENTHESIZED expression, e.g. (R0)/(R12).
* IBM's machine-operator processor (IEUF8M) routes every register field
* through the general expression evaluator (IEUF8V), which handles
* parenthesized grouping, so `(R0)` evaluates to register 0.  The structured
* -macro decks emit this via EXITIF (TRB,(R0),...) -> STKINS -> PUSHINS ->
* `TRB (R0),...`.  The parenthesized form must assemble identically to the
* bare form.
         BALR  R1,0
         USING *,R1
BARE     TRB   R0,X'2411'
PAREN    TRB   (R0),X'2411'
EXPR     TRB   (R0+1),X'2411'
R0       EQU   0
R1       EQU   1
         END
