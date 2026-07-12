* RS "no-R1" system ops take an ADDRESS operand; their R1 slot is a FIXED part of
* the opcode (LM=4, STM=0, LDM=0, LPS=5, SVC=1), confirmed against PASS MAFGEN
* flight listings (mafgen/DASS_G16.ASC): LM->CCFB, STM->C8FB, LDM->68FB, LPS->CDFB,
* SVC->C9FB.  These share opcode 11001..11111 (LM/STM/LPS/SVC) and are distinguished
* solely by that fixed R1 field, which the descriptor bakes -- so they encode from
* the no-x {a,b} RS descriptor (has_rs_descriptor now accepts the a/b form).
*
* This guards a real correctness fix: LM and STM had no implied R1, fell through the
* RS/SRS form selection, and emitted ZERO bytes; an explicit-R1 SVC collided with
* STM.  Now all five assemble to their flight encodings (first halfword pinned by
* the golden; the byte-exact field encodings are in asm101_compiled_vectors).
RSNOR1   CSECT
         USING RSNOR1,3
TGT      DC    X'0000'
         LM    TGT
         STM   TGT
         LDM   TGT
         LPS   TGT
         SVC   TGT
         END
