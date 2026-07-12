* Compile-fixpoint convergence with a MACRO-EXPANDED multiply-defined EQU.
* The DUPEQU macro emits `DUP EQU *`; invoking it twice defines DUP at two
* different offsets (mirroring the real BILDNEW5 DCHAR/MSG13x message-table
* labels).  The two definitions write conflicting `*` addresses on every
* compile pass while the end-of-pass table (last-def-wins) is stable.  A
* per-EQU "value changed" repeat trigger reacts to that intra-pass churn
* forever and pinned the assembly at the 50-pass maxPasses cap; the
* end-of-pass snapshot fixpoint settles it instead.  This deck must converge
* (no "did not converge" warning) and assemble cleanly.
T        CSECT
         DUPEQU
         DUPEQU
USEDUP   DC    Y(DUP)
         END
