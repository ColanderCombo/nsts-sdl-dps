PROCTEST CSECT
* Self-contained slice of the MLIB80 PROCTEST diagnostic deck.  The PROC /
* EXECUTE / ENDPROC structured-macro system DELIBERATELY emits `MNOTE 4` for
* malformed input.  This deck drives every error path so the macro engine's
* diagnostics are pinned: exactly the labelled bad lines below must produce a
* severity-4 MNOTE, and the valid pairings must assemble cleanly (no MNOTE).
* See [[asm101_nested_sublist_macarg_bug]] / the BILDNEW5 campaign.
         BALR  REG1,0
* --- valid: a defined procedure called and returned with a matching reg ---
         PROC  CHUVA
         ENDPROC CHUVA,REG7
         EXECUTE CHUVA,REG7
* --- ERROR: procedure name omitted (EXECUTE with no operand) ---
EX_NONAM EXECUTE
* --- ERROR: return register omitted (EXECUTE name only) ---
EX_NOREG EXECUTE ONCA
* --- ERROR: procedure name > 8 chars (AMIGODAONCA = 11) ---
EX_LONGN EXECUTE AMIGODAONCA,REG2
* --- ERROR: return register > 8 chars (REGISTER2 = 9) ---
EX_LONGR EXECUTE ONCA,REGISTER2
* --- ERROR: ENDPROC name omitted ---
         PROC  SAPATO
EP_NONAM ENDPROC
* --- ERROR: ENDPROC return register omitted ---
EP_NOREG ENDPROC SAPATO
* --- ERROR: ENDPROC name > 8 chars (SAPATEIRO = 9) ---
EP_LONGN ENDPROC SAPATEIRO,REG3
* --- ERROR: ENDPROC return register > 8 chars (REGISTER3 = 9) ---
EP_LONGR ENDPROC SAPATO,REGISTER3
* --- ERROR: return register mismatch (TRISTEZA defined REG4, executed REG3) -
         PROC  TRISTEZA
         ENDPROC TRISTEZA,REG4
EX_MISM1 EXECUTE TRISTEZA,REG3
* --- ERROR: return register mismatch (JANELA executed REG5 then REG0) -------
* JANELA is forward-referenced by the EXECUTEs; the PROC below defines the
* symbol (JANELA DS 0H) so the first EXECUTE's BAL target resolves -- only the
* REG0/REG5 mismatch on the second EXECUTE is the intended diagnostic.
EX_MISM2 EXECUTE JANELA,REG5
EX_MISM3 EXECUTE JANELA,REG0
         PROC  JANELA
         ENDPROC JANELA,REG5
REG0     EQU   0
REG1     EQU   1
REG2     EQU   2
REG3     EQU   3
REG4     EQU   4
REG5     EQU   5
REG6     EQU   6
REG7     EQU   7
         END
