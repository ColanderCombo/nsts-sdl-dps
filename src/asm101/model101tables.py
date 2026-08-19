#!/usr/bin/env python3
#
# Tables and instruction generation routines
# 
# This file is based on ASM101S/model101tables.py from virtualagc by Ronald Burkey:
#   https://github.com/virtualagc/virtualagc/blob/master/ASM101S/model101Tables.py
# 
# It's been significantly refactored for asm101, most of the encoding moving
# to instrdefs.py/instrset.py which reads instruction descriptors from 
# intr_defs.json
#
from .instrdefs import (CPU, CPU_DEFS, MSC_DEFS, BCE_DEFS,
                        has_rs_descriptor, rs_hw1,
                        RS_HW2_EXTENDED, RS_HW2_INDEXED,
                        cpu_mnemonics, cpu_optype, cpu_fp,
                        CPU_ALIASES)
from .iop_instr import ALIASES as _MSC_ALIASES

# Macro-language op families, shared by the macro generator (assemble.Assemble)
# and codegen's 'ignore' set (model101) so their memberships cannot drift.
# DECLARE = global/local symbolic-variable declarations, SET = assignments;
# GENERATOR_OPS = the conditional-assembly directives the generator performs --
# they emit no object code and are dropped from the stream the assembly stage
# reads (the IBM F-assembler evaluates these and writes only generated
# statements to SYSUT2).  MNOTE and SPACE are excluded: both are
# listing-relevant, and MNOTE carries diagnostics.
MACRO_DECLARE_OPS = frozenset({"GBLA", "GBLB", "GBLC", "LCLA", "LCLB", "LCLC"})
MACRO_SET_OPS = frozenset({"SETA", "SETB", "SETC"})
GENERATOR_OPS = (MACRO_DECLARE_OPS | MACRO_SET_OPS
                 | frozenset({"AIF", "AGO", "ANOP", "ACTR", "MEXIT"}))

# CPU RR-instruction DISPATCH membership -- a derived SET of mnemonics (encoding
# comes from the GPC descriptors in instrdefs), not an opcode table.  Three parts:
#   * cpu_mnemonics("RR")  -- the descriptor-typed RR instructions;
#   * BRANCH_ALIASES_R | {BR, NOPR}  -- register-form branch extended mnemonics with
#     no descriptor of their own; they encode through BCR's descriptor with the
#     implied condition mask in R1 (see model101.py);
#   * CPU_ALIASES  -- LACR, which encodes through LCR's descriptor.
# BRANCH_ALIASES_R is defined further down, so ARGS_RR is assembled there.
#
# Though technically RR, LFXI/LFLI/SPM/BR/NOPR are special: encoded slightly
# differently, or aliases that accept an altered syntax.

SHIFT_OPERATIONS = frozenset(n for n in CPU_DEFS if cpu_optype(n) == "SHFT")
# The field values are the masks.
BRANCH_ALIASES = {"B": 7, "BR": 7, "NOP": 0, "NOPR": 0, "BH": 1, "BL": 2,
                 "BE": 4, "BNH": 6, "BNL": 5, "BNE": 3, "BO": 1, "BP": 1,
                 "BM": 2, "BZ": 4, "BNP": 6, "BNM": 5, "BNN": 5, "BNZ": 3,
                 "BNO": 6, "BLE": 6, "BN": 2, "BHE": 5, "BNC": 6,
                 # Overflow/carry branches (BVCF family, NOT condition-code).
                 # Mask bits per the BVC M1 field: 1=overflow, 2=carry.  These
                 # route to BVCF (not BCF) -- see model101.py SRS-forward path.
                 "BOV": 1, "BOC": 2 }

# Register-form conditional branches (e.g. BNZR R7 == BCR 3,R7): extended
# mnemonics for BCR where the implied R1 holds the condition-code mask and the
# single operand is the R2 register holding the branch target.  Derived from the
# BC masks in BRANCH_ALIASES, skipping stems that already have register forms
# (B->BR, NOP->NOPR) and the overflow/carry family (BO/BNO/BNC), which map to
# BVCR rather than BCR.
BRANCH_ALIASES_R = {
    stem + "R": mask for stem, mask in BRANCH_ALIASES.items()
    if stem not in ("B", "BR", "NOP", "NOPR", "BO", "BNO", "BNC", "BOV", "BOC")
}
# Assemble the full RR dispatch set now that BRANCH_ALIASES_R exists (see ARGS_RR
# comment above).
ARGS_RR = cpu_mnemonics("RR") | set(BRANCH_ALIASES_R) | {"BR", "NOPR"} | set(CPU_ALIASES)

# An op takes the @/# (indirect/immediate) RS addressing variants iff it has an RS
# long form (has_rs_descriptor) -- i.e. a base register; the shifts and the SRS-only
# branch forms (BCF/BCB/BCTB/BVCF) do not.  BVC takes @/# like its structural twins
# BC/BAL/BCT/BIX (POO-confirmed).
_AT_POUND_BASES = frozenset(n for n in cpu_mnemonics("SRS", "RS") if has_rs_descriptor(n))
_AT_POUND_VARIANTS = frozenset(b + s for b in _AT_POUND_BASES for s in ("@", "#", "@#"))

# The SRS-typed branch aliases (B/BH/../NOP/BO/BOV/BOC); BR/NOPR are RR (in ARGS_RR).
_SRS_BRANCH_ALIASES = frozenset(BRANCH_ALIASES) - {"BR", "NOPR"}

_CC_BRANCH_ALIASES = frozenset(BRANCH_ALIASES) - {"BR", "NOPR", "BOV", "BOC", "BNC"}

# The overflow/carry branch family: BVC and its extended mnemonics.  These test
# the carry and overflow indicators rather than the condition code
OVFL_CARRY_BRANCHES = frozenset({"BNC", "BOV", "BOC", "BVC"})
BRANCH_AT_POUND_VARIANTS = frozenset(b + s for b in _CC_BRANCH_ALIASES
                                  for s in ("@", "#", "@#"))

# SRS-only: SRS-typed ops with no RS long form -- the branch forms BCF/BCB/BCTB/BVCF
# and the shifts -- plus NOP (a mask-0 branch alias with only an SRS form).
ARGS_SRS_ONLY = frozenset(n for n in cpu_mnemonics("SRS")
                        if not has_rs_descriptor(n)) | {"NOP"}
# Both forms (SRS short AND RS long): the SRS-typed memory ops (A/L/ST/../ZH/TD/
# TH/SHW), the condition/overflow branch aliases, and BC/BVC (the two RS-typed
# branches whose mnemonic also resolves to an SRS short form).
ARGS_SRS_AND_RS = (frozenset(n for n in cpu_mnemonics("SRS") if has_rs_descriptor(n))
                | (_SRS_BRANCH_ALIASES - {"NOP"}) | {"BC", "BVC"})

# RS-only (forceRS): the RS-typed ('/X') bare ops EXCEPT BC/BVC, plus every @/#
# addressing variant (always RS, AM=1)
ARGS_RS_ONLY = ((frozenset(cpu_mnemonics("RS")) - {"BC", "BVC"})
              | _AT_POUND_VARIANTS | BRANCH_AT_POUND_VARIANTS)

ARGS_SRS_OR_RS = ARGS_SRS_AND_RS | ARGS_SRS_ONLY | ARGS_RS_ONLY

# RI / SI dispatch is DERIVED from the GPC descriptors by type; only membership
# matters.  SHI is the one extra: no descriptor of its own (rewritten to AHI with
# the immediate negated before encoding).  LHI is RI-typed but assembles as the RS
# instruction LA; both are special-cased in the RI codegen path.
ARGS_RI = cpu_mnemonics("RI") | {"SHI"}
ARGS_SI = cpu_mnemonics("SI")

### IOP - MSC & BCE
ARGS_MSC = frozenset(MSC_DEFS) | frozenset(_MSC_ALIASES)
ARGS_BCE = frozenset(BCE_DEFS)

KNOWN_INSTRUCTIONS = (set(ARGS_RR) | set(ARGS_SRS_OR_RS) | set(ARGS_RS_ONLY)
                     | set(ARGS_SRS_ONLY) | set(ARGS_SI) | set(ARGS_MSC)
                     | set(ARGS_BCE))

FP_OPERATIONS_SP = frozenset(n for n in CPU_DEFS if cpu_fp(n) == "SP")

# Parsing rules for different operations: the pseudo-ops, then one rule per
# machine-instruction dispatch set.
APPROPRIATE_RULES = {
    "ENTRY": "idlist", "EXTRN": "idlist",
    "EQU": "equ", "USING": "exprs", "DROP": "exprs",
    "DC": "dc", "DS": "ds",
} | {
    op: rule
    for ops, rule in ((ARGS_RR, "rr"), (ARGS_SRS_OR_RS, "rs"),
                      (ARGS_RI, "ri"), (ARGS_SI, "si"),
                      (ARGS_MSC, "msc"), (ARGS_BCE, "bce"))
    for op in ops
}

def _recordAdr2(stmt, d2, when=True):
  # Listing 2nd-address column: record the resolved operand address when it
  # differs from adr1 (set by the caller).  SRS gates it on an extra `when`.
  if when and stmt.adr1 is not None and stmt.adr1 != d2:
    stmt.adr2 = d2

def generateSRS(stmt, mnemonic, r1, d2, b2):
  desc = CPU.by_name.get(mnemonic)
  if desc is None or desc.nbits != 16 or "d" not in desc.fields:
    raise ValueError(f"generateSRS: {mnemonic} has no SRS short descriptor")
  #   * standard x/d/b ops          -> {x, d, b};
  #   * shifts (SLL/SRA/..) and SRS branch forms (BCF/BVCF/BCB/BCTB) ->
  #     {x, d} -- the shift-type / branch-form discriminator is in FIXED
  #     bits, so the caller's b2 (shift type / form bits) is baked in;
  #   * no-R1 ops (TD/TH/ZH/SHW)    -> {d, b} -- the implied R1 is in FIXED
  #     bits, so the caller's r1 is baked in.
  data = bytearray(CPU.encode(mnemonic, {"x": r1, "d": d2, "b": b2}))
  _recordAdr2(stmt, d2, b2 == 3 or stmt.operation in BRANCH_ALIASES)
  return data

def generateRS0(stmt, mnemonic, r1, d2, b2):
  # RS AM=0 (extended): first halfword + a 16-bit displacement.
  if not (has_rs_descriptor(mnemonic) and 0 <= b2 <= 7):
    raise ValueError(f"generateRS0: {mnemonic} has no RS descriptor (base {b2!r})")
  data = bytearray(rs_hw1(mnemonic, r1, b2, indexed=False)
                   + RS_HW2_EXTENDED.pack({"d": d2}))
  _recordAdr2(stmt, d2)
  return data


def generateRS1(stmt, mnemonic, ia, i, r1, d2, x2, b2):
  # RS AM=1 (indexed): first halfword + a second halfword carrying the index
  # register (x2), the ia/i mode bits, and the 11-bit displacement.
  if not (has_rs_descriptor(mnemonic) and 0 <= b2 <= 3):
    raise ValueError(f"generateRS1: {mnemonic} has no RS descriptor (base {b2!r})")
  data = bytearray(rs_hw1(mnemonic, r1, b2, indexed=True)
                   + RS_HW2_INDEXED.pack({"x": x2, "a": ia, "i": i, "d": d2}))
  _recordAdr2(stmt, d2)
  return data
    
