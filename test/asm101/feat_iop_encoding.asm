* Exercises the IOP (MSC/BCE) encoding wired into model101 (descriptor-driven via
* iop_instr).  Covers: short literal/immediate ops; PC-relative memory + branch ops
* (d = target-(IC+1)) including condition-code aliases; the 1-bit index flag "(1)";
* long-format 18-bit absolute addresses (a) with Y-type RLDs for relocatable and
* EXTRN targets; the two-operand @LBP/@CALL (b/t + a) forms; and BCE #MIN/#MOUT/#BU.
* Byte values are cross-checked by decoding each emitted word back through the
* flight-validated iop_instr encoder (see the test harness); addressing matches the
* PASS/BFS flight semantics (FIOMCNTL etc.).
IOPTEST  CSECT
* --- short: immediates / no-displacement ---
         @LI   0
         @LI   -1
         @SIO  0
* --- PC-relative branches (condition baked by alias) + explicit @BC ---
         @B    TGT
         @BZ   TGT
         @BNZ  TGT
         @BC   5,TGT
         @BXZ  TGT
* --- PC-relative memory ops, plus the index flag "(1)" ---
         @L    FW
         @A    FW
         @ST   FW
         @ST   FW(1)
         @N    FW
* --- long: 18-bit absolute address (local -> RLD) ---
         @LF   FW
         @STF  FW
         @BU   TGT
         @BU@  TGT
* --- long: EXTRN target (-> RLD against the external symbol) ---
         @LF   EXTSYM
* --- long: two-operand forms (b/t + a) ---
         @LBP  6,FW
         @CALL 3,TGT
TGT      @REC  FW
FW       DS    1F
* --- BCE: absolute branch, message in/out, two-operand ---
         #BU   TGT
         #MIN  3,5
         #MOUT 7,X'1A'
* --- BCE: #DLY delay-from-memory, PC-relative d (FCMMGBOV pattern -> C800, d=0) ---
GNDLC    #DLY  GNDLC+1
* --- BCE short memory-reference ops: PC-relative d (+ m index flag).  Targets are
* --- written as HERE+-const so the expected word is layout-independent: a self -
* --- reference (HERE+1) gives d=0; HERE-4 gives d=(HERE-4)-(HERE+1)=-5=0x7FB.
WIXHERE  #WIX  WIXHERE-4           d=-5 -> 27FB (BFS DKWIX/FCWIX are pc-rel)
SSCM1    #SSC  SSCM1-4(1)          m=1, d=-5 -> 4FFB
SSCM0    #SSC  SSCM0-4             m=0, d=-5 -> 47FB
SSTM1    #SST  SSTM1-4(1)          m=1, d=-5 -> 5FFB (flight-exact: FIOMMUPG)
LTOHERE  #LTO  LTOHERE+1           d=0  -> B800 (flight-exact: BILDNEW5)
         EXTRN EXTSYM
         END
