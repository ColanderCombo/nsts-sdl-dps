"""mafgen -- replacement for the flight MAFGEN (Memory Analysis File GENerator).

Reads a composed memory-configuration image (mmu2fcm's <CFG>.fcm +
<CFG>.sym.json) plus the per-phase build sidecars (PHASEnn sym.json, SDFLIB)
and emits a DASS-style listing that can be diffed against the flight MAFGEN
listings in pfs/PFS/mafgen/DASS_*.ASC.

Sections emitted:
  * the alphabetical csect -> address index
  * the address-ordered csect extent list
  * the MEMORY MAP dump: csect headers, AP-101S disassembly with symbolic
    comments and register tracking, the SDF data-symbol walk, the asm
    statement-stream replay, and the error-simulation diagnostics
  * --strip normalizes a real DASS .ASC to the same shape so the two line up

Sections not emitted: MAP SUMMARY, CSECT CROSS-REFERENCE INDEX, HAL/S %MACRO
and SVC LOCATION INDEX, INSTRUCTION FREQUENCIES, PATCH SUMMARY, RUN
STATISTICS.  --score therefore compares only the sections above.
"""
