* Symbol attribution and RLD relocation ESDIDs across a SECOND control
* section.  The expression evaluator re-projects every relocatable value
* onto the FIRST csect and gives each RLD site back its true section by
* range, so a module with two csects exercises three separate paths:
*   * 'EQU *' must be filed against the csect it is WRITTEN in, at that
*     section's own offset -- not against the first csect at a module-global
*     halfword count.  A forward reference to one from the first csect also
*     has to survive pass 3, before the second csect is re-walked.
*   * a reference from inside a secondary csect to a symbol in that SAME
*     csect must relocate against it, not against the first csect.
*   * an EXTRN referenced from a secondary csect must relocate against the
*     external, whose addend is not an offset into any csect at all.
* All three are invisible while the linker keeps the csects contiguous, and
* wrong by the placement gap when an OVERLAY boundary separates them.
SEC1     CSECT
         EXTRN EXTSYM
S1START  DC    Y(1)
S1EQU    EQU   *
         DC    Y(2)
         DC    Y(S2EQU)      forward ref to an 'EQU *' in the 2nd csect
         DC    Y(S2LABEL)    forward ref to a DC label in the 2nd csect
SEC2     CSECT
S2LABEL  DC    Y(3)
S2EQU    EQU   *
         DC    Y(4)
         DC    Y(S2EQU)      self-reference: must relocate against SEC2
         DC    Y(S2LABEL)    self-reference: must relocate against SEC2
         DC    Y(S1EQU)      back-reference into the FIRST csect
         DC    Y(0)          filler: keep the A-con fullword aligned
         DC    A(EXTSYM)     external referenced from a secondary csect
         END
