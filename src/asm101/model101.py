'''
  model101: Guts of the AP-101 shuttle instruction set assembler

  This file is based on ASM101S/model101.py from virtualagc by Ronald Burkey:
    https://github.com/virtualagc/virtualagc/blob/master/ASM101S/model101.py
  
  It's been significantly refactored for asm101.  A summary of major changes:

  - Instead of using random hashes for tracking relocations, we model them
    more in line with AS037F1's IEUF8V RLIST.  'Reloc' objects track
    and calculate relocations

  - We introduce dataclasses for Sections, SymtabEntrys, Literals,
    Relocations and Statements, instead of using raw dicts.
    
  - Main loop split into bite-sized handlers
  
  - Instead of encoding manually we use descriptors extracted from
    nsts-sim-gpc, passing through CPU.encode; see instrdefs.py
    
  - We use the lark library for parsing instead of tatsu.  tatsu was falling
    off a cliff and having issues with parsing large files with complex macros.
    Most of the rules are defined as LALR (very fast), with only a few using
    earley (slower) when necessary.  We also generate a single AST in Pass 0
    instead of reparsing.
  
  - RS->SRS optimization rework, modeled on AS037F1's operand decomposition
    procedure (IEUF8M DECOMP: resolve USING base+displacement, smallest
    displacement wins, higher register on ties), so the optimizer and codegen
    resolve identically, matching the original AP-101 assembler.
  
  - IOP instruction support added
  
  - add ORG/CNOP/@CNOP/#CNOP
  
  - Better RLD generation
  
  - L.n bitfields, B-types, F scale modifier, handle double negative '--'
    L' lengths, duplication for all types
    
  - '$' suffix to force long RS form; BVC/BOV/BOC overflow/carry branches
    (SRS BVCF form); register-form branch aliases (BR/NOPR/BNZR/...) via BCR

  - Handle unnamed DSECT, picking right CSECT when preceded by DSECT,
    EQU * handling in secondary CSECTS

  - Deterministic (by reference) ordering of ESD cards in output module.
   
  - When generating SRS displacement values for base-relative/USING, 
    don't subtract IC
    
  - EQU forward references, allow multiple EXTERNS of the same symbol.
'''

import re
import sys
import copy
from dataclasses import dataclass, field
from .expressions import error, \
    evalArithmeticExpression, asmContext, NO_LOCALS, Reloc
from .larkparse import parse
from .astnodes import Int, zRelocTarget
from .statement import Statement
from ap101Utils.addr import Addr
from ap101Utils.addrcon import ZCon
from ap101Utils.ibmhex import toFloatIBM

'''
SRS / RS / AM form selection (the source of most special-casing below)
-----------------------------------------------------------------------
Many mnemonics assemble to more than one machine form, and the source usually
doesn't say which (exceptions: '$' forces long format; '@'/'#'/an explicit
index force RS AM=1):
    SRS       -> 1 halfword  (6-bit displacement, 2-bit base)
    RS AM=0   -> 2 halfwords (16-bit displacement, 3-bit base)
    RS AM=1   -> 2 halfwords (11-bit displacement, index/indirect/immediate)
The assembler picks the shortest form that can encode a statement, by the
same procedure Assembler F uses to pick a base register.  The operand is
decomposed into base + displacement a la AS037F1's IEUF8M DECOMP
('unUsing'/'findB2D2': same-section USING entries only, smallest displacement
wins, higher-numbered register on ties), and SRS is chosen iff the decomposed
displacement fits its 6-bit unitized field (see "D2 units" below);
index/indirect/immediate addressing and in-range PC-relative operands take
RS AM=1, 16-bit absolute displacements RS AM=0.

The sizing is span-dependent: the fit test needs symbol addresses, and
addresses depend on which earlier instructions condensed to SRS.  The
multi-pass loop resolves it by relaxation to a fixpoint (code only shrinks,
so it converges).
'optimizeScratch' and 'handleSrsOrRs' carry the form-selection logic (scratch
entries not yet sized are flagged "ambiguous"); a few corners ('forceAM0',
the FP-op exclusions) remain empirical, tuned to reproduce the original
assembler's bytes exactly.  SRS_CEILING=56 matches the POO's register-
designated shift-count threshold (see 'SHIFT_REG_COUNT_BASE'), applied
uniformly by the original assembler.

Passes (an initial macro/field pass already ran before generateObjectCode):
    Pass 0  parse each operand to an AST, once, so pass-1 lookahead is cheap;
            also create preliminary symtab entries (provisional 4-byte sizes)
            so pass 1 can resolve forward references (e.g. USING).
    Pass 1  place sections and resolve symbol addresses; size SRS-vs-RS where
            it can, looking ahead over the pass-0 ASTs for the ambiguous ones.
    Pass 2  repeat pass 1 using the sizes pass 1 settled on, fixing up the
            addresses pass 1 got wrong.
    Pass 3  emit object code and data; repeats until addresses stop moving
            (forward branches condense RS->SRS, shrinking code monotonically).

Reloc (defined in astnodes.py, re-exported by expressions.py)
-------------------------------------------------------------
Before linking we know only section-relative offsets, so a value is a numeric
part plus a multiset of relocation terms {esd_id: coefficient}:
    bare int                absolute (no terms)
    Reloc(off, {id: +1})    simply relocatable -> one RLD
    anything else           complex (coef != 1, or multi-term) -> no single RLD
Python +, -, *, unary - implement the relocatability algebra (mirroring the IBM
F-assembler's IEUF8V RLIST): terms cancel to a bare int when a result is
absolute (e.g. A-B within one section).  'unhash'/'classify' read the class
back out; 'getHashcode' assigns each CSECT/DSECT/EXTRN an internal esd_id
(unrelated to the ESD ids the object writer later emits).

D2 units (halfword vs fullword)
-------------------------------
Only the SRS short form scales its 6-bit D2 by operand size: the POO's SRS EA
is base + (D2 << (addrWidth-1)), so fullword ops count D2 in fullwords and
halfword ops in halfwords.  Both RS forms take HALFWORD displacements
regardless of operand size -- AM=0 a full 16-bit one, AM=1 an 11-bit one
(POO: "the alignment of the displacement is the same whether addressing
double word, fullword or halfword operands"; cf. g_EA in the gpc simulator).
A fullword-D2 op whose displacement is an odd number of halfwords therefore
cannot be SRS ('forbiddenSRS' below) and must take an RS form.  WHICH
mnemonics are fullword-D2 comes from the descriptors' 'addrWidth' attribute
(see 'srs_d2_units') -- the very field the simulator's g_EA scales by, so the
assembler and the simulator cannot disagree.
'''
# SRS displacement-fit bounds.  The ceiling is 56, not 64: D values 56-63
# are the POO's register-designated SHIFT-count form (D = 56+n -> count in
# bits 10-15 of Rn; 6246156B p.78).  The original assembler applied the
# 56 ceiling to every SRS op.
SRS_FLOOR = 0
SRS_CEILING = 56
# FORWARD branches condense to the SRS short form only when the SELF-
# CONDENSED displacement (the distance after the branch itself shrinks a
# halfword) is below 54, not the full 56-halfword field: the assembler
# reserves margin on forward distances (decided before the target
# settles); backward distances are exact at decision time and use the
# full window.  Callers subtract 1 from a currently-RS-sized branch's
# measured distance before this test.
SRS_FORWARD_BRANCH_CEILING = 54
SHIFT_REG_COUNT_BASE = 56


# SRS branch displacement-fit tests, shared by the pass-1 ambiguity resolver
# (optimizeScratch) and the codegen (handleSrsOrRs) so both make the same
# form decision.  'd' is the PC-relative halfword distance (updated-IC
# convention).
def fitsForwardSrsBranch(d, stmt):
  # A branch currently sized RS (4 bytes) shrinks a halfword when condensed,
  # so its measured distance gets a 1-halfword credit before the ceiling.
  return d >= SRS_FLOOR and \
      d - (1 if stmt.length == 4 else 0) < SRS_FORWARD_BRANCH_CEILING


def fitsBackwardSrsBranch(d):
  return d < 0 and d > -SRS_CEILING

# Internal ESD-id <-> symbol/section maps.  Each CSECT/DSECT and each EXTRN
# symbol gets a unique positive id (the relocation-term key in a 'Reloc'); the
# id is sequential and deterministic.  'esdidToName' lets 'unhash' map a single
# surviving relocation term back to its section/EXTRN name.
esdidToName = {}
nameToEsdid = {}
def getHashcode(symbol):
  esdid = nameToEsdid.get(symbol)
  if esdid is None:
    esdid = len(nameToEsdid) + 1
    nameToEsdid[symbol] = esdid
    esdidToName[esdid] = symbol
  return Reloc(0, {esdid: 1})

# Split an evaluated value into (section, number): section is None for an
# absolute result (number is the value), the EXTRN/CSECT name for a simply
# relocatable one, and None,None for a complex one (not a single +1 term).
def unhash(value):
  if not isinstance(value, Reloc):
    return None, value          # a bare int is absolute
  terms = value.terms             # already only nonzero coefficients
  if len(terms) == 1:
    (esdid, coef), = terms.items()
    if coef == 1 and esdid in esdidToName:
      return esdidToName[esdid], value.num
  return None, None               # complex (coef != 1, multi-term, or POISON)

# Relocatability class of an evaluated value, via 'unhash':
#   'absolute' - a bare int (no relocation terms)
#   'simple'   - one CSECT/EXTRN, +1 coefficient -> emits one RLD
#   'complex'  - did NOT reduce to a single section (A*2, A+B, or a cross-section
#                difference): no single RLD can express it, so callers flag it
#                rather than emit a garbage value.
# Only values that reach a relocation-consuming site (one that calls unhash) get
# classified; a complex value that never reaches one is not caught.
# No production caller; kept as documentation of the taxonomy above.
def classify(value):
  sect, off = unhash(value)
  if sect is not None:
    return "simple"
  if off is not None:
    return "absolute"
  return "complex"

# The bare numeric part of a value (the displacement/address), regardless of
# whether it is relocatable ('Reloc') or absolute (a plain int).  Used at the
# instruction-encoding sites that need the numeric image of a displacement.
def _num(value):
  return value.num if isinstance(value, Reloc) else value


# True when the operand's D2 is a bare numeric displacement (an Int leaf).
# A numeric D2 condenses to SRS in both the 'expr(,Rn)' and 'expr(Rn)' forms;
# optimizeScratch and handleSrsOrRs share this test so both make the same
# form decision.
def isNumberD2(ast):
  return isinstance(ast.get("D2"), Int)

# Similar to 'unhash', but uses the 'USING' list, and returns a pair
# B2,D2 (or None,None in case of error).  This is the AP-101 analogue of
# AS037F1's IEUF8M DECOMP: same-section USING entries only (the subtraction
# must cancel to a bare int), smallest displacement <= 'limit' wins, highest
# register on ties ('<=' -- DECOMP rejects only a strictly-higher
# displacement).  Also the body of 'findB2D2', whose limit is the 12-bit
# displacement field's 4095.
def unUsing(using, hashed, limit=0xFFFFFF):
  b2 = None
  d2 = None
  for i in range(len(using)):
    u = using[i]
    if u == None:
      continue
    # 'hashed - base' cancels the section term to a bare int only when both
    # are in the same section; a still-relocatable difference (cross-section)
    # is a 'Reloc', never a valid base displacement, so skip it.
    j = hashed - u[0]
    if not isinstance(j, int) or j < 0 or j > limit:
      continue
    if d2 == None or j <= d2:
      b2 = i
      d2 = j
  return b2,d2

# Re-resolve each USING base of a collect-pass snapshot against the CURRENT
# symtab.  A pass-1 USING whose base symbol is a forward reference resolves
# against the pre-pass 'preliminary' symtab entries -- byte-unit position
# over-estimates -- while optimizeScratch evaluates instruction operands
# against settled pass-1 values.  Mixing the two skews every displacement
# under that USING (e.g. RUNASM SQRT's 'USING A,R1' with A defined later in
# #LSQRT: base A recorded at +4, settled A at +2 -> 'A R6,A' computed d2=-2,
# SRS wrongly rejected).  The SRS/RS size decision must compare like with
# like, so recompute the base the same way codegen eventually will.
def refreshUsing(using):
  fresh = []
  for u in using:
    if u is None or len(u) < 7 or u[3] is None:
      fresh.append(u)
      continue
    h, section, address, locAst, k, ustmt, star = u
    h2 = evalArithmeticExpression(locAst, NO_LOCALS, ustmt, symtab, star,
                                  severity=0)
    if h2 is None:
      fresh.append(u)
      continue
    h2 = h2 + 4096 * k
    s2, a2 = unhash(h2)
    fresh.append((h2, s2, a2 if a2 is not None else address))
  return fresh

# Resolve a combined halfword offset back to the actual CSECT that contains it,
# for building an RLD relocation.  Returns the name of the (non-DSECT) section
# whose range [offset, offset + used//2) contains 'value', or 'default' if none
# does.  'exclude' (a section name) never matches -- pass the current section
# when the value must resolve to a *different* CSECT, or None to allow any.
def resolveCSect(value, sects, default, exclude=None):
  for sn, sd in sects.items():
    if sd.dsect or sd.offset is None:
      continue
    so = sd.offset.hw
    # Mid-pass, a LATER section's 'used' is still growing (it resets with
    # pos1 each compile pass); the previous pass's 'usedPrev' is the size
    # estimate for ranging.
    if so <= value < so + max(sd.used.hw, sd.usedPrev.hw) and sn != exclude:
      return sn
  return default


def sectionOffset(name):
  """Halfword offset of section 'name' within the contiguous CSECT layout,
  or 0 if it is unknown or not yet placed."""
  if name in sects and sects[name].offset is not None:
    return sects[name].offset.hw
  return 0


def makeExtern(symbol, stmt=None):
  """Create the EXTERNAL symtab entry for 'symbol' (a hashcoded external
  reference).  Bookkeeping in 'extrns'/'rextrns' is left to the caller, since
  its timing differs between call sites."""
  entry = SymtabEntry(type="EXTERNAL", value=getHashcode(symbol))
  if stmt is not None:
    entry.properties = stmt
  symtab[symbol] = entry


def registerExtrn(symbol, n=None):
  """Register 'symbol' as an external reference: list it in 'extrns' (keyed
  to source position 'n', first reference wins), create its EXTERNAL symtab
  entry if it has no entry at all, and index its hashcode in 'rextrns'.  A
  symbol already defined as something other than EXTERNAL is only listed in
  'extrns' -- its value is not a bare external hashcode, so it gets no
  'rextrns' slot."""
  extrns.setdefault(symbol, n)
  if symbol not in symtab:
    makeExtern(symbol)
  if symtab[symbol].type == "EXTERNAL":
    rextrns[symtab[symbol].value] = symbol


def applyResolvedValue(entry, value):
  """If 'value' is simply relocatable, record its section/address/dsect on the
  symtab 'entry'.  Returns the section name when relocatable, else None."""
  section, offset = unhash(value)
  if section is not None:
    entry.section = section
    entry.address = offset
    entry.dsect = sects[section].dsect
  return section

@dataclass
class Relocation:
  """One RLD relocation the assembler emits: a reference to 'symbol', patched at
  'address' (an 'Addr' -- RLD offsets are bytes) within control section
  'section'.  'type' is the AP-101 relocation kind -- 'Y' (halfword address),
  'A' (ordinary 4-byte adcon), or 'Z' (Z-constant) -- which the object writer
  maps to the RLD flag byte it punches."""
  symbol: str
  section: str
  address: Addr
  type: str = 'Y'


# Append one RLD relocation entry.  Every site shares the same shape; only the
# referenced 'symbol', the patched 'address', and the relocation 'type' vary.
def appendReloc(relocations, symbol, section, address, type='Y'):
  relocations.append(Relocation(symbol, section, address, type))

# Write 'value' big-endian into buf[ptr : ptr+nbytes] and return the advanced
# pointer.  Used to lay DC values down into the data buffer.
def packBE(buf, ptr, value, nbytes):
  if ptr + nbytes > len(buf):
    # A DC suboperand list can outgrow the initial allocation; grow it.
    # Bytes past the final ptr are never emitted.
    buf.extend(bytearray(ptr + nbytes - len(buf) + 1024))
  j = (nbytes - 1) * 8
  for _ in range(nbytes):
    buf[ptr] = (value >> j) & 0xFF
    ptr += 1
    j -= 8
  return ptr

def decimalExponentInt(fstr):
  """An IBM fixed-point constant with a decimal exponent whose value is an
  exact integer -- F'900E6' is 900*10**6 = 900000000 (FCMCBLKS FPM15MIN,
  IBM 35A4E900), not a fraction of full scale.  Returns None for any other
  form (no exponent, or a non-integral value), which keeps the existing
  integer/fraction paths untouched."""
  m = re.match(r"^([+-]?)(\d+)(?:\.(\d*))?E([+-]?\d+)$", fstr)
  if not m:
    return None
  sign, whole, frac, exp = m.groups()
  frac = frac or ""
  e = int(exp) - len(frac)
  digits = int(whole + frac) if whole + frac else 0
  if e < 0:
    p = 10 ** -e
    if digits % p:
      return None
    digits //= p
  else:
    digits *= 10 ** e
  return -digits if sign == "-" else digits


def parseDcLength(length):
  """Parse a DC/DS length modifier (the suboperand 'length' field, e.g.
  ('L','2') or ('L','.','7'), or None).  Returns (value, isBits, err): 'value'
  is the integer length, 'isBits' is True for the sub-byte "L.n" bit-length
  form, and 'err' is True if the modifier is present but couldn't be parsed.
  An absent modifier returns (None, False, False)."""
  if not length:
    return None, False, False
  t = length
  if not isinstance(t, (tuple, list)) or len(t) < 2 or t[0] != "L":
    return None, False, True
  parts = list(t[1:])
  isBits = False
  if parts and parts[0] == ".":
    isBits = True
    parts = parts[1:]
  sign = 1
  if parts and parts[0] in ("+", "-"):
    if parts[0] == "-":
      sign = -1
    parts = parts[1:]
  if len(parts) == 1 and isinstance(parts[0], str) and parts[0].isdigit():
    return sign * int(parts[0]), isBits, False
  return None, isBits, True

def parseSignedInt(s):
  """Parse an integer string that may carry a RUN of leading signs, folding
  them (each '-' flips sign).  This is how the POS-generated "XPOS -&L" path
  arrives: for a negative coordinate &L, the operand text is "--285", which a
  DC F/H value (e.g. FL.11'--285') must read as +285.  Returns the int, or
  None if 's' is not a (possibly multiply-signed) integer."""
  s = s.strip()
  neg = False
  i = 0
  while i < len(s) and s[i] in "+-":
    if s[i] == "-":
      neg = not neg
    i += 1
  digits = s[i:]
  if not digits.isdigit():
    return None
  v = int(digits)
  return -v if neg else v

from .model101tables import (
    APPROPRIATE_RULES, ARGS_BCE, ARGS_MSC, ARGS_RI, ARGS_RR, ARGS_RS_ONLY,
    ARGS_SI, ARGS_SRS_AND_RS, ARGS_SRS_ONLY, ARGS_SRS_OR_RS, BRANCH_ALIASES,
    BRANCH_ALIASES_R, BRANCH_AT_POUND_VARIANTS, FP_OPERATIONS_SP,
    GENERATOR_OPS, KNOWN_INSTRUCTIONS, SHIFT_OPERATIONS, generateRS0,
    generateRS1, generateSRS,
)
from .instrdefs import (
    AP101S_ONLY, CPU, base_op, rs_form_bits, rs_hw1, implied_r1, srs_d2_units,
)
from . import iop_instr

# RR mnemonics whose R1 is implied by the mnemonic itself (so an explicit R1
# operand is an error): SPM's R1 is unused, NOPR is BC 0,R2 (mask 0) and BR is
# BC 7,R2, both routed via BCR.  The register-form conditional branches
# (BNZR, BZR, ...) likewise imply their condition mask -- see 'BRANCH_ALIASES_R'.
RR_IMPLIED_R1 = {"SPM": 0, "NOPR": 0, "BR": 7}


@dataclass
class Section:
  memory: bytearray
  esdSeq: int                     # source-appearance position (ESD order key)
  pos1: Addr = Addr(0)            # location counter (a byte address)
  used: Addr = Addr(0)            # high-water mark: live bytes of 'memory'
  usedPrev: Addr = Addr(0)        # previous compile pass's final 'used' --
                                  # the size ESTIMATE mid-pass readers (the
                                  # findB2D2 current-section fallback,
                                  # resolveCSect) need while this pass's
                                  # 'used' is still growing.  'used' itself
                                  # resets with pos1 at each compile pass, so
                                  # a pass that converges SMALLER doesn't
                                  # ratchet the csect size (a stale larger
                                  # size leaves a phantom trailing zero
                                  # halfword).
  scratch: list = field(default_factory=list)
  dsect: bool = False
  offset: Addr | None = None      # final origin in the contiguous CSECT
                                  # layout; None until laid out


@dataclass
class SymtabEntry:
  """One 'symtab' entry -- a defined name (label, section, EQU, or external),
  replacing the legacy loose dict.  'type' is the discriminator (CSECT / DATA /
  INSTRUCTION / EQU / EXTERNAL); the other fields are filled in incrementally
  across passes, so all are optional and the entry is MUTATED in place (not
  frozen) as the symbol resolves.  'None' means "not set yet", matching the
  old dict's key-absence semantics -- so a value of 0 is still "set".

  Distinct from the per-instruction SCRATCH dicts on 'Section.scratch' (keys
  pos1/length/ambiguous/using/...), which stay plain dicts and are NOT this type.

    value             the symbol's value: an int or a relocatable 'Reloc'
    section           name of the control section the symbol lives in
    address           halfword offset of the symbol within its section
    dsect             True if the symbol's section is a DSECT
    alignment         the symbol's byte alignment (data/instruction symbols)
    preliminary       True for a placeholder created before its real definition
    n                 source position (stmt.n) that defined the symbol
    preliminaryOffset  section's provisional byte origin (section entries)
    references        source positions referencing the symbol (None until first;
                      lazily created, matching the old 'setdefault')
    entry             True if the symbol is an ENTRY point
    properties        back-reference to the defining 'Statement'"""
  type: str
  value: object = None
  section: str | None = None
  address: int | None = None
  dsect: bool | None = None
  alignment: int | None = None
  preliminary: bool | None = None
  n: int | None = None
  preliminaryOffset: int | None = None
  references: list | None = None
  entry: bool | None = None
  properties: object = None


# Reserved internal section name for the blank-name "unnamed dummy section"
# (a bare 'DSECT').  Distinct from "" (the unnamed CSECT) and unparseable as a
# symbol, so it never collides with a real section; DSECTs emit no ESD, so it
# stays internal.
UNNAMED_DSECT = "\x00DSECT"

sects = {} # CSECTS and DSECTS.
# 'entries' (ENTRY/LD) and 'extrns' (EXTRN/ER) are insertion-ordered dicts used
# as ordered sets (value unused): iteration order = first-appearance order, so
# the ESD is emitted deterministically and in IBM OS/360 Assembler-F order (it
# assigns ESD IDs at first reference, ascending, with no sort -- IEUF7E SETESD).
entries = {} # For 'ENTRY'.
extrns = {} # For 'EXTRN'.
rextrns = {} # For 'EXTRN'
symtab = {}
relocations = [] # RLD entries
metadata = {
    "sects": sects,
    "entries": entries,
    "extrns": extrns,
    "rextrns": rextrns,
    "symtab": symtab,
    "relocations": relocations,
    "passCount": 0
    }

# CSECT/DSECT memory grows in fixed zero-filled chunks.
MEMORY_CHUNK_SIZE = 4096
DEFAULT_CHUNK = [0x00]*MEMORY_CHUNK_SIZE


def ensureSection(name, dsect, esdSeq, stmt=None, preliminary=None):
  """Create control section 'name' -- its 'Section' and its CSECT symtab
  entry -- if it doesn't exist yet.  'esdSeq' is the source position used for
  ESD ordering.  Pass-0 callers mark the symtab entry preliminary and attach
  the defining 'stmt'; the main-pass caller leaves both unset."""
  if name in sects:
    return
  sects[name] = Section(memory=bytearray(DEFAULT_CHUNK), dsect=dsect,
                        esdSeq=esdSeq)
  if name not in symtab:
    symtab[name] = SymtabEntry(
        type="CSECT",
        section=name,
        address=0,
        value=getHashcode(name),
        dsect=dsect,
        preliminary=preliminary,
        n=stmt.n if stmt is not None else None,
        properties=stmt)

'''
'literalPools' tracks System/360 "literals" -- operands like =1234, =X'ABCD',
=Y(label) -- which the assembler stores in a pool and references by address
rather than as immediate data.  A literal always lands in the pool opened by
the *following* 'LTORG' (or the default pool at the end of the first CSECT if
none follows), so literals are forward references until that pool is placed.
Duplicates are merged within one LTORG-to-LTORG interval but kept per pool
across intervals; two literals with equal value but different attributes (e.g.
=X'1234' vs =X'0000001234', different lengths) are not duplicates.  =A(...)
literals are unsupported.

There are N+1 'LiteralPool's for N 'LTORG's, the last being the default pool.
A new pool starts assigned to CSECT "" with no offset yet; the real CSECT and
offset are filled in when its 'LTORG' (or end of source) closes it out.
Literals are laid out by descending alignment (8, 4, 2, 1) then order of
appearance, not in source order.
'''
@dataclass
class Literal:
  """Literal value:
    value      the literal's arithmetic value (l2) before length/scale shaping;
               0 for a character literal (which has no arithmetic value).
               Refreshed in place across passes for a forward =Y(expr) whose
               label resolves late.
    T          the constant-type letter (C/X/B/H/F/E/D/Y/Z)
    L          the literal's length in bytes -- its layout footprint in the pool
    operand    normalized operand text (e.g. =Y(LEND-LBEG)); the STABLE identity
               key the pool dedups on -- value shifts pass-to-pass, operand
               doesn't (see 'literalIndex')
    assembled  the encoded bytes (bytearray); refreshed alongside 'value'
    zsym       for a =Z literal, the reloc-target symbol name (the pool slot
               gets a 'Z' RLD against it at flush time); None otherwise
    zdata      for a =Z literal, True when the target is the DATA address
               subfield ('=Z(,sym...)') -- the pool-slot RLD is ZCON/data
               ('ZD', 0x50) so the linker patches DSR, not ZCON/code"""
  value: object
  T: str
  L: int
  operand: str
  assembled: bytearray
  zsym: object = None
  zdata: bool = False

@dataclass
class LiteralPool:
  """One literal pool -- the =constants gathered up to one 'LTORG' (or, for
  the final pool, to end-of-source):
    sect       name of the CSECT containing the pool ("" until closed out)
    offset     byte offset ('Addr') of the pool within 'sect'; None until its
               'LTORG' (or end of source) closes it out
    alignment  pool alignment (2, 4, or 8), from its widest literal
    offsets    per-literal byte offsets into the pool, parallel to 'literals';
               filled in only after the discovery pass -- kept separate from
               the 'Literal' objects so a literal can be looked up without
               already knowing its offset
    size       total pool size in bytes
    literals   the unique 'Literal's ('literalIndex' defines the identity)
    index      operand text -> position in 'literals'; maintained by 'add'
               ('literals' is append-only, never reordered)
    atLtorg    True for a pool closed by an explicit mid-csect LTORG (its
               origin is the LTORG's location and the LC advances past it);
               False for the default end-of-source pool (placed after the
               csect's high-water mark)"""
  sect: str = ""
  offset: Addr | None = None
  alignment: int | None = None
  offsets: list = field(default_factory=list)
  size: int = 0
  literals: list = field(default_factory=list)
  index: dict = field(default_factory=dict)
  atLtorg: bool = False

  def add(self, literal):
    self.index[literal.operand] = len(self.literals)
    self.literals.append(literal)

# Arrange a pool's literals by alignment bucket (8/4/2/1) and compute their
# in-pool byte offsets, the pool's own alignment, and its total size.  Shared
# by 'ltorg' (a mid-csect pool's size must be known AT the LTORG, so the
# location counter can advance past it) and the end-of-pass-1 layout of the
# default end-of-source pool.
def layoutPool(pool):
  offset = 0
  pool.alignment = 2
  pool.offsets = [None] * len(pool.literals)
  pool.size = 0
  for alignment in [8, 4, 2, 1]:
    for i, lit in enumerate(pool.literals):
      if pool.offsets[i] != None:
        continue
      if (lit.L % alignment) != 0:
        continue
      if alignment > pool.alignment:
        pool.alignment = alignment
      rem = offset % alignment
      if rem != 0:
        offset += alignment - rem
      pool.offsets[i] = offset
      offset += lit.L
      if offset > pool.size:
        pool.size = offset

literalPools = [LiteralPool()]
# Call this whenever an 'LTORG' is encountered.  The closed pool's contents
# are complete here, so it is laid out immediately -- handleLtorg needs its
# size to advance the location counter past it.
def ltorg(sect):
  literalPool = literalPools[-1]
  literalPool.sect = sect
  literalPool.offset = sects[sect].pos1
  literalPool.atLtorg = True
  layoutPool(literalPool)
  literalPools.append(LiteralPool())
# Call this at the end of the source code.
def endOfSource():
  literalPool = literalPools[-1]
  if not literalPool.literals:
    literalPools.pop()
    return
  for sect in sects:
    if not sects[sect].dsect:
      literalPool.sect = sect
      literalPool.offset = sects[sect].pos1
      return
# Index into 'literalPool.literals' of the literal matching 'literal', or -1,
# keyed by OPERAND text rather than whole-object equality: a =Y(expr) literal's
# value changes across passes as forward labels resolve, so whole-object
# matching would mistake the same slot for a new literal.  The operand text is
# the stable identity and the slot's length (its layout footprint) is
# value-independent, so refreshing a slot's value in place is layout-safe.
def literalIndex(literalPool, literal):
  return literalPool.index.get(literal.operand, -1)

@dataclass(frozen=True)
class DcType:
  """Static per-constant-type properties -- the single source for "how wide /
  how aligned is type T", shared by 'evalLiteralAttributes', 'handleDcDs'
  (including its L'-attribute capture), and 'packBitLengthDC'.  'nbytes' is
  None for the string-y types (C/X/B) whose width comes from the data itself;
  'fullScale' is the fixed-point full-scale multiplier for the fraction forms
  of H/F."""
  nbytes: int | None
  align: int
  fullScale: int | None = None

DCTYPES = {
    "C": DcType(None, 1),
    "X": DcType(None, 1),
    "B": DcType(None, 1),
    "H": DcType(2, 2, fullScale=1 << 15),
    "F": DcType(4, 4, fullScale=1 << 31),
    "E": DcType(4, 4),
    "D": DcType(8, 4),      # even doublewords are aligned to fullword
    "Y": DcType(2, 2),
    "A": DcType(4, 4),
    "Z": DcType(4, 4),
}

# Evaluates the AST returning either a 'Literal' for the literal pool or
# None.  'ast["L2"]' is the lconstant node -- an
# 'Lcon(type, body, length, scale, raw)' dataclass the Lark front-end emits.
def evalLiteralAttributes(stmt, ast, symtab):
  lit = ast["L2"]
  t, body, length, lscale, raw = lit.type, lit.body, lit.length, lit.scale, lit.raw
  if t == "C":
    # A character literal has no arithmetic value (evalArithmeticExpression
    # rejects it), so skip that path; the C branch below encodes EBCDIC
    # bytes directly.
    l2 = 0
  else:
    l2 = evalArithmeticExpression(lit, NO_LOCALS, stmt, symtab, star=None, severity=0)
    if l2 == None:
      return None
  scale = 1
  if lscale is not None:
    scale = pow(2.0, -lscale)
  numerical = True
  zsym = None        # set by the Z branch (=Z literal reloc-target symbol)
  zdata = False      # set by the Z branch (data-subfield target -> 'ZD' RLD)
  if t == "C":
    numerical = False
    value = body.replace("''", "'").replace("&&", "&")
    l = len(value)
    bytes = bytearray(value.encode("ebcdicvagc"))
  elif t == "B":
    l = (len(body) + 7) // 8
    value = l2
  elif t == "X":
    l = (len(body) + 1) // 2
    value = l2
  elif t in ("H", "F"):
    l = DCTYPES[t].nbytes
    fullScale = DCTYPES[t].fullScale
    value = l2 * scale
    if value > -1.0 and value < 1.0:
      value *= fullScale
      if value >= fullScale:
        value = fullScale - 1
      elif value <= -fullScale:
        value = -fullScale + 1
    value = round(value)
  elif t in ("E", "D"):
    l = DCTYPES[t].nbytes
    # =E rounds at the 24-bit single-precision fraction, like DC E.
    msw, lsw = toFloatIBM(body, scale, single=(t == "E"))
    value = msw if t == "E" else (msw << 32) | lsw
  elif t == "Y":
    l = DCTYPES[t].nbytes
    value = l2
  elif t == "Z":
    # A =Z literal is a ZCON pool entry: 4 bytes [HW0 addend][flags][0].  HW0
    # holds the addend pre-link; a 'Z' RLD against the symbol is emitted at the
    # pool slot (flush time) so the linker adds the resolved symbol address.
    # Build the bytes here (not via the generic numerical packing, which would
    # spread the addend across all 4 bytes).
    l = DCTYPES[t].nbytes
    zname, zadd = zRelocTarget(body)
    if zadd is None:
      zadd = 0
    zflags = 0
    if lit.flags is not None:
      zflags = evalArithmeticExpression(lit.flags, NO_LOCALS, stmt, symtab,
                                        star=None, severity=0)
      if zflags is None:
        zflags = 0
    value = zadd
    numerical = False
    bytes = ZCon.prelink(zadd, zflags).to_bytes()
    zsym = zname
    zdata = getattr(lit, "zdata", False)
  else:
    error(stmt, f"Unknown constant-type specifier '{t}'")
    return None
  if numerical:
    # A relocatable literal (e.g. =Y(label)) contributes its numeric image;
    # the literal pool stores the in-section value (no RLD at the pool site).
    value = _num(value)
    bytes = bytearray(l)
    for i in range(l - 1, -1, -1):
      bytes[i] = value & 0xFF
      value = value >> 8
  if length is not None:
    # Note that we treat the length modifier as a count of halfwords
    # rather than bytes, in contradiction to the System/360 assembly-
    # language manual.
    l = 2 * length
    if l < len(bytes):
      if numerical:
        bytes = bytes[-l:]
      else:
        bytes = bytes[:l]
    while l > len(bytes):
      if numerical:
        bytes.insert(0, 0)
      else:
        bytes.append(0x40) # EBCDIC space
  operand = f"={t}"
  if length is not None:
    operand += f"L{length}"
  if lscale is not None:
    operand += f"S{lscale}"
  if t in ("Y", "Z"):
    # =Y(expr)/=Z(...): the body is an arith AST, not a quoted string --
    # reconstruct the parenthesized form from the captured source text so the
    # pool key / listing string round-trip faithfully.
    operand += f"({raw if raw is not None else ''})"
  else:
    operand += f"'{body}'"
  return Literal(value=l2, T=t, L=l, operand=operand, assembled=bytes,
                 zsym=zsym, zdata=zdata)

#=============================================================================
# 'optimizeScratch' analyzes the "scratch" structures created during the
# "collect" pass, deciding for each not-yet-sized (flagged "ambiguous")
# instruction whether it condenses to the SRS short form or stays RS: the
# operand is decomposed to base + displacement (DECOMP-style, via 'unUsing')
# and SRS is chosen when the unitized displacement fits.  See the "SRS / RS /
# AM form selection" note at the top of the file.

def optimizeScratch():
  def adjust(scratch, stmt, i):
    entry = scratch[i]
    entry["length"] = 2
    stmt.length = 2
    entry["ambiguous"] = False
    lastEntry = entry
    for j in range(i+1, len(scratch)):
      entry2 = scratch[j]
      if sect == entry2["sect"]:
        nextPos1 = lastEntry["pos1"] + lastEntry["length"]
        lastEntry = entry2
        # alignment is a real Statement field (default 2); the >2 test
        # already no-ops the unaligned case, so no presence guard needed.
        alignment = entry2["properties"].alignment
        if alignment > 2:
          nextPos1 = nextPos1.align(alignment)
        nextPos2 = nextPos1.hw
        entry2["pos1"] = nextPos1
        entry2["properties"].pos1 = nextPos1
        if "name" in entry2 and entry2["properties"].operation != "EQU":
          # An EQU symbol's value comes from its operand expression, not its
          # position, so don't overwrite it here -- the outer walk re-evaluates
          # it on arrival.  A forward-referencing EQU (see the EQU branch
          # below) may not even be in symtab yet.
          name = entry2["name"]
          sym2 = symtab[name]
          sym2.address = nextPos2
          # Keep the symbol's section term(s); replace only its offset.
          _sv = sym2.value
          sym2.value = Reloc(nextPos2, _sv.terms) \
              if isinstance(_sv, Reloc) else nextPos2
    
  literalPoolNumber = 0
  for sect in sects:
    scratch = sects[sect].scratch
    for i in range(len(scratch)):
      entry = scratch[i]
      stmt = entry["properties"]
      operation = stmt.operation
      if operation == "EQU":
        if "name" not in entry:
          continue
        if stmt.ast is None:
          error(stmt, "Could not parse operand of EQU")
          continue
        v = evalArithmeticExpression(stmt.ast.v, NO_LOCALS, \
                                     stmt, symtab, \
                                     symtab[sect].value + entry["pos1"].hw, \
                                     severity=0)
        n = entry["name"]
        if n not in symtab:
          # A forward-referencing EQU (e.g. 'ICCLGTH EQU ICCEND-ICCSTRT'
          # with ICCEND defined later) whose value subfield could not be
          # evaluated at its position in the main pass -- handleEqu returns
          # early without registering it.  By the time optimizeScratch runs
          # the later operands are in symtab, so the re-eval here succeeds;
          # register the symbol now rather than crash on the missing entry
          # (the next pass's handleEqu defines it canonically).
          symtab[n] = SymtabEntry(type="EQU", value=v, properties=stmt)
        symtab[n].value = v
        if applyResolvedValue(symtab[n], v) is not None:
          symtab[n].properties = stmt
        continue
      elif operation == "LTORG":
        # A mid-csect LTORG occupies its pool's size (see handleLtorg); its
        # scratch length must survive into stmt.length so the end-of-pass-1
        # length replay keeps the pool's room.
        stmt.length = entry["length"]
        literalPoolNumber += 1
        continue
      stmt.length = entry["length"]
      if not entry["ambiguous"]:
        continue
      # We've found an ambiguous instruction (i.e., SRS vs RS) that we
      # must resolve.  First thing is that we have to parse the operand
      # to determine the target address we want to reach.
      ast = stmt.ast
      if ast == None or "X2" in ast:
        entry["ambiguous"] = False
        continue
      if "B2" in ast:
        b2 = evalArithmeticExpression(ast["B2"], NO_LOCALS, stmt, \
                                      symtab, \
                                      symtab[sect].value + entry["pos1"].hw, \
                                      severity=0)
        if b2 in [4, 5, 6, 7]:
          # B2 was really X2.
          entry["ambiguous"] = False
          continue
      # An AST with neither L2 nor D2 leaves d2 unset; None drops it below.
      d2 = None
      if "L2" in ast:
        attributes = evalLiteralAttributes(stmt, ast, symtab)
        if attributes == None:
          entry["ambiguous"] = False
          continue
        literalPool = literalPools[literalPoolNumber]
        index = literalIndex(literalPool, attributes)
        if index < 0:
          error(stmt, "Literal not in literal pool")
          entry["ambiguous"] = False
          continue
        offset = literalPool.offsets[index] + literalPool.offset
        d2 = symtab[literalPool.sect].value + offset.hw
      elif "D2" in ast:
        d2 = evalArithmeticExpression(ast["D2"], NO_LOCALS, stmt, \
                                      symtab, \
                                      symtab[sect].value + entry["pos1"].hw, \
                                      severity=0)
      if d2 == None:
        entry["ambiguous"] = False
        continue
      section, value = unhash(d2)
      if section == None:
        # The COMMA form 'expr(,Rn)' (B2ONLY -- index slot explicitly
        # omitted) never condenses to SRS, even when the value fits; the
        # bare form 'expr(Rn)' DOES condense.  Number displacements
        # condense in both forms (legacy).
        if "B2" in ast and (isNumberD2(ast) or not ast.get("B2ONLY")) \
                and value >= SRS_FLOOR \
                and value < SRS_CEILING and operation != "BCT":
          adjust(scratch, stmt, i)
          continue
        entry["ambiguous"] = False
        continue
      # Special cases that branch backward:
      if section == sect and \
              (operation in BRANCH_ALIASES or operation in ["BCT", "BC", "BVC"]):
        d = symtab[sect].value + stmt.pos1.hw + 1 - d2
        if d >= SRS_FLOOR and d < SRS_CEILING:
          adjust(scratch, stmt, i)
          continue
      if operation == "BCT":
        entry["ambiguous"] = False
        continue
      # Check for the case 'OPCODE R1,D2', where 'D2' is a location in
      # the current CSECT.
      if section == sect and \
              (operation in BRANCH_ALIASES or operation in ["BC", "BVC"]):
        d = value - stmt.pos1.hw - 1
        if fitsForwardSrsBranch(d, stmt):
          adjust(scratch, stmt, i)
          continue
      # A paren register on a RELOCATABLE address ('L R2,KFSTRNG1(X3)',
      # 'ST R4,TESTBUF(R3)') is an INDEX -- the base still comes from USING,
      # and SRS has no index field, so base+index+displacement can only be
      # the long form.  (With an ABSOLUTE d2 the paren register is the BASE
      # and the section==None branch above already handles the SRS case.)
      # Before 'refreshUsing' the stale snapshot bases masked this path by
      # rarely matching; an accurate base would wrongly condense these.
      if "B2" in ast:
        entry["ambiguous"] = False
        continue
      # 'OPCODE R1,D2' where D2 lives in a CSECT/DSECT currently in 'USING'.
      # Condense to SRS only when the ACTUAL displacement fits.  'unUsing'
      # resolves the very base/displacement codegen will encode (the
      # min-displacement register), so the optimizer's size choice and
      # codegen can never disagree.  The earlier heuristic tested the USING
      # *base address* instead -- which for a DSECT USING (base 0) always
      # looked in-range and wrongly condensed large DSECT-member offsets
      # (e.g. 'STH R1,TICCSYTM' over 'USING TFICC,R2', offset ~100) to SRS,
      # then failed at codegen with "SRS displacement out of range".
      ub2, ud2 = unUsing(refreshUsing(entry["using"]), d2)
      if ub2 is not None:
        # Match codegen's SRS test exactly: SRS only when the unitized
        # displacement is in range AND divides evenly (else forbiddenSRS).
        dUnitizer = srs_d2_units(operation)
        dSRS = (ud2 + dUnitizer - 1) // dUnitizer
        if dSRS >= SRS_FLOOR and dSRS < SRS_CEILING and ud2 % dUnitizer == 0:
          adjust(scratch, stmt, i)
          continue
  return

#=============================================================================
# Generate object code for AP-101.

'''
Generate object code in place on 'source' -- a list of per-line 'Statement'
objects (macros already expanded, continuation lines already merged into the
first card, so only pure assembly remains).  Each line's Statement is only
added to: the 'errors' field may grow (via 'error') and unset fields may be
filled in to hold object-code data; no other existing field is touched.
'macros' is just the list of operation names to ignore.  Returns a dict of
whole-assembly metadata.  Addresses are in bytes.
'''
# The macro-language ops (shared base sets in model101tables) plus the
# listing- and assembly-control pseudo-ops that generate no object code and
# do not move the location counter.  COPY is handled in the source-reading
# pass (its target is already expanded inline), so by the time code is
# generated the COPY statement itself is inert.  Mutable: each run adds its
# macro names.
ignore = set(GENERATOR_OPS) | { "TITLE", "SPACE", "MNOTE", "SPON", "SPOFF",
                                "EJECT", "PRINT", "COPY" }


def normalizeLongFormat(operation, stmt):
  """
  Handle the '$' long-format modifier on an instruction mnemonic.

  A trailing '$' (e.g. "LA$", "B$") forces the instruction to its RS (long,
  fullword) encoding instead of letting the assembler pick the shortest (SRS,
  halfword) form.  Strip the '$' to the base mnemonic, record the request in
  stmt.longFormat, and return the base.  Anything else (including a
  bare '$' or a '$' on a non-instruction) is returned unchanged so it still
  surfaces as an error later.
  """
  if operation.endswith("$") and operation[:-1] in KNOWN_INSTRUCTIONS:
    operation = operation[:-1]
    stmt.operation = operation
    stmt.longFormat = True
  return operation

'''
Whole-assembly data structures.  'symtab' maps each symbol to a 'SymtabEntry'
whose 'type' is "EXTERNAL" (EXTRN), "DATA" (DS/DC), "INSTRUCTION" (program
label), "CSECT" (CSECT/DSECT/START), or "EQU".  There is no "ENTRY" type -- an
ENTRY symbol keeps its other type and gains 'entry=True'.  (Literals live in
'literalPools', not here.)

Other fields: 'section' (containing CSECT), 'address' (halfword offset within
it), and 'value', used when the symbol appears in an arithmetic expression:
  - None for symbols not usable in expressions;
  - for address symbols (EXTERNAL/DATA/INSTRUCTION/ENTRY/CSECT), the Reloc
    value combining 'section' and 'address' (decode with 'unhash');
  - for EQU, an absolute int or a Reloc, whichever its definition resolves to.
'''

# Scratch buffer for assembling one 'DC'.  1024 bytes is far below the real
# maximum but ample in practice.
dcBuffer = bytearray(1024)
firstCSECT = None
# The statement-stream prologue shared by the pass-0 and main pass loops.
# Yields (stmt, operation) for each statement a pass should process, applying:
#   * continuation consumption FIRST, before 'skip' -- a continuation card is
#     frequently skip-flagged, so testing 'skip' first would leave the flag
#     stuck True and swallow the next real statement (e.g. an 'ORG *-1');
#   * macro-continuation absorption -- a continued macro invocation's
#     continuation cards sit AFTER its expansion (skip-flagged), so its flag
#     must not absorb the first expanded statement (unless definition/comment
#     lines happen to interleave and soak it up -- FCMBMTMC's BMTENT survives
#     only because its body opens with comment cards);
#   * definition/comment skips, then the '$' long-format normalization.
def processable(source, macros, heartbeat=None):
  continuation = False
  for n, stmt in enumerate(source):
    if heartbeat is not None:
      heartbeat(n)
    if continuation:
      continuation = stmt.continues
      continue
    if stmt.skip:
      continue
    continuation = stmt.continues and stmt.operation not in macros
    if stmt.inMacroDefinition or stmt.is_blank_or_comment:
      continue
    yield stmt, normalizeLongFormat(stmt.operation, stmt)


def generateObjectCode(source, macros, log=None, march="ap101s"):
  global dcBuffer, firstCSECT, literalPools

  # Optional progress callback (e.g. Assemble._log under --verbose).  The pass
  # loop below can run for many seconds on large inputs with nothing else
  # to show for it, so it reports each pass as it starts; default is a no-op.
  if log is None:
    log = lambda _message: None

  #-----------------------------------------------------------------------
  # Setup

  collect = False
  asis = False
  compile = False
  sect = None # Current section.
  for key in macros:
    ignore.add(key)
  stmt = Statement()
    
  name = ""
  operation = ""
  using = [None]*8
  # Write cursor into the shared 'dcBuffer', used while assembling one DC.
  # Bound here in 'generateObjectCode''s scope so the 'handleDcDs' and
  # 'emitAddressConstant' nested handlers can share it via 'nonlocal' (the
  # DC/DS handler resets it to 0 per statement before writing).
  dcBufferPtr = 0
  # CSECT(True)-vs-DSECT(False) flag for the current section.  Bound here so
  # 'commonProcessing' and the 'handleCsectDsect' handler share it via
  # 'nonlocal'; it is always set (by a CSECT/DSECT line or commonProcessing's
  # implicit-default-section path) before any reader runs.
  cVsD = False

  #-----------------------------------------------------------------------

  # Write to the current CSECT/DSECT, or (for DS) just reserve space.  'bytes'
  # is a bytearray (DC: the actual bytes) or an int (DS: a byte count to
  # reserve).  Caller must have aligned already.
  def toMemory(bytes, alignment = 1):
    nonlocal collect, asis, compile, stmt, name, operation
    if sect is None or sect not in sects:
      return
    pos1 = sects[sect].pos1
    if collect:
      newScratch = {}
      if name != "":
        newScratch["name"] = name
        newScratch["alignment"] = alignment
      if isinstance(bytes, int):
        newScratch["length"] = bytes
      else:
        newScratch["length"] = len(bytes)
      newScratch = newScratch | {
          "ambiguous": (operation in ARGS_SRS_AND_RS or operation == "BCT") \
                       and not stmt.longFormat,
          "pos1": pos1,
          "sect": sect,
          "operation": operation,
          "operand": operand,
          "properties": stmt,
          "using": copy.copy(using)
          }
      sects[sect].scratch.append(newScratch)
      stmt.scratch = newScratch
    stmt.section = sect
    stmt.pos1 = pos1
    if isinstance(bytes, bytearray):
      end = pos1 + len(bytes)
      if cVsD and compile:
        memory = sects[sect].memory
        while end > len(memory):
          memory.extend(DEFAULT_CHUNK)
        memory[pos1:end] = bytes
      stmt.assembled = bytes
      sects[sect].pos1 = end
    else:
      sects[sect].pos1 += bytes
    if sects[sect].pos1 > sects[sect].used:
      sects[sect].used = sects[sect].pos1

  # Common processing for all instructions. The 'alignment' argument is one
  # of 1 (byte), 2 (halfword), 4 (word), 8 (doubleword).
  def commonProcessing(alignment=1):
    global firstCSECT
    nonlocal cVsD, sect, name, operation

    # Make sure we're in *some* CSECT or DSECT
    if sect == None:
      cVsD = True
      sect = ""
      firstCSECT = sect
      asmContext.firstCSECT = firstCSECT
      if sect not in sects:
        # Implicit default CSECT: code that precedes any explicit CSECT,
        # so it appears before everything (esdSeq -1; ESD is emitted in
        # source-appearance order).
        sects[sect] = Section(memory=bytearray(DEFAULT_CHUNK), esdSeq=-1)
        
    # Perform alignment.
    if alignment > 1:
      if alignment > stmt.alignment:
        stmt.alignment = alignment
      # Don't use toMemory() here: it can have unintended side effects
      # such as assigning statements like "DS 0F" a non-zero length.
      pos1 = sects[sect].pos1.align(alignment)
      if pos1 != sects[sect].pos1:
        # A CPU-csect alignment gap (DC/DS/LTORG forcing fullword or
        # doubleword alignment from an odd halfword) is covered by TXT and
        # filled with C9FB -- the SVC opcode halfword, a stray-execution
        # trap -- NOT zeros (HAL-compiler csect gaps use C6C6 instead).
        # CNOP is excluded: it fills its own gap with executable NOPs
        # (D800; C000 for @CNOP/#CNOP) before calling here.
        if cVsD and compile and \
                operation not in ("CNOP", "@CNOP", "#CNOP"):
          memory = sects[sect].memory
          while pos1 > len(memory):
            memory.extend(DEFAULT_CHUNK)
          for p in range(int(sects[sect].pos1), int(pos1)):
            memory[p] = 0xC9 if (p & 1) == 0 else 0xFB
        sects[sect].pos1 = pos1
        if pos1 > sects[sect].used:
          sects[sect].used = pos1
        
    # Add 'name' (if any) to the symbol table.  Runs in the collect passes
    # (which build the table) and again in compile: passes 1-2 only
    # over-estimate label addresses (forward branches not yet condensed
    # RS->SRS), so compile re-derives each address from the now-shorter code
    # and the outer loop repeats until the table settles (end-of-pass
    # snapshot below).  Code only shrinks, so addresses fall monotonically
    # and it converges.
    if (collect or compile) and name != "":
      pos2 = sects[sect].pos1.hw
      # In collect, a real "address shouldn't change" check; in compile we
      # deliberately re-derive it each pass.  Convergence is judged once
      # per pass at the end of the loop, not per line -- a multiply-defined
      # label writes conflicting addresses within a pass and would never
      # look settled otherwise.
      if not compile and name in symtab and symtab[name].preliminary is None:
        oldSect = symtab[name].section
        oldPos = symtab[name].address
        if oldSect != sect or oldPos != pos2:
          error(stmt,
                f"Symbol {name} address has changed: ({oldSect},{oldPos}) -> ({sect},{pos2})")
      _e = symtab[name]
      _e.section = sect
      _e.address = pos2
      _e.value = symtab[sect].value + pos2
      _e.dsect = sects[sect].dsect
      if operation in ["DC", "DS"]:
        symtab[name].type = "DATA"
      else:
        symtab[name].type = "INSTRUCTION"
    
  # Gets the hashed address of the current program counter.  Before the first
  # CSECT there is no location counter (e.g. an absolute equate such as a
  # register definition, 'R6 EQU 6', ahead of any section), so the current
  # location is simply 0 -- mirroring toMemory's 'sect is None' guard.
  def currentHash():
    if sect is None or sect not in sects:
      return 0
    return symtab[sect].value + sects[sect].pos1.hw

  def emitDisplacementReloc(symbol):
    """Emit a Y-type RLD against 'symbol' for the instruction's displacement
    field -- two bytes into the instruction at the section's current location."""
    appendReloc(relocations, symbol, sect, sects[sect].pos1 + 2)

  def emitAddressConstant(suboperand, nbytes, rldType, typeName):
    """Assemble a DC A(expr)/Y(expr) address-constant suboperand: evaluate
    each term to an 'nbytes'-wide value and, at pass 3, emit an 'rldType' RLD
    for any simply-relocatable term (complex relocatable terms are rejected).
    The A and Y constants differ only in nbytes (4 vs 2), rldType, and the
    diagnostic 'typeName'."""
    nonlocal dcBufferPtr
    for exp in suboperand.values:
      v = evalArithmeticExpression(exp, NO_LOCALS, stmt, symtab, currentHash())
      if v == None:
        error(stmt, f"Cannot evaluate {typeName} constant")
        v = 0
      relSect, relOff = unhash(v)
      if relSect is not None and passCount == 3:
        combinedOffset = relOff + sectionOffset(relSect)
        rldSymbol = resolveCSect(combinedOffset, sects, relSect, exclude=sect)
        appendReloc(relocations, rldSymbol, sect,
                    sects[sect].pos1 + dcBufferPtr, type=rldType)
        v = combinedOffset
      elif relSect is None and relOff is None and passCount >= 3:
        # classify(v) == 'complex': A*2, A+B, Y(A*2), etc.
        error(stmt, "Complex relocatable expression not "
              f"supported in {typeName} constant")
      # 'v' may still be a Reloc (simple but not yet pass 3, or complex);
      # packBE writes its low 'nbytes', which is its numeric image.
      dcBufferPtr = packBE(dcBuffer, dcBufferPtr, _num(v), nbytes)

  # Assemble a DC/DS whose suboperands use bit-length ("L.n") modifiers by
  # packing them into a contiguous MSB-first bit stream, zero-padded to the
  # next byte boundary (manual: "Bit-Length Specification").  Suboperands
  # without a bit length contribute their full byte-width as bits, so mixed
  # forms like 'AL.16(SYM),X'0001'' pack correctly.  A relocatable symbol is
  # given an RLD only when its field is byte-aligned and byte-width (e.g.
  # AL.16 == a 2-byte Y reloc); in a sub-byte field it is assembled absolute
  # and flagged (severity 0), since the linker cannot patch sub-byte fields.
  def packBitLengthDC(stmt, operation, flattened):
    commonProcessing(1)
    if sect is None or sect not in sects:
      return
    startBytePos = sects[sect].pos1
    acc = {"bits": 0, "nbits": 0}

    def fieldBits(stype, width, isBits):
      if isBits:
        return width
      if width is not None:
        return width * 8
      # The type's own byte width (DCTYPES) in bits; the data-width types
      # (C/X/B, nbytes None) default to one byte.
      dct = DCTYPES.get(stype)
      return dct.nbytes * 8 if dct is not None and dct.nbytes else 8

    def appendBits(value, width):
      if width <= 0:
        return
      acc["bits"] = (acc["bits"] << width) | (value & ((1 << width) - 1))
      acc["nbits"] += width

    for suboperand in flattened:
      if suboperand.dup is None:
        dup = 1
      else:
        dup = evalArithmeticExpression(suboperand.dup, NO_LOCALS, stmt,
                                       symtab, currentHash())
        if dup is None:
          error(stmt, "Could not evaluate duplication factor")
          dup = 1
      stype = suboperand.type
      width, isBits, lerr = parseDcLength(suboperand.length)
      if lerr:
        error(stmt, "Could not evaluate length modifier")
        continue
      field = fieldBits(stype, width, isBits)

      # Build (value, fieldbits, relocatableHashcode|None) for one copy.
      contributions = []
      if stype in ("Y", "A"):
        for exp in suboperand.values:
          v = evalArithmeticExpression(exp, NO_LOCALS, stmt, symtab,
                                       currentHash())
          if v is None:
            error(stmt,
                  f"Cannot evaluate {stype}-type constant")
            contributions.append((0, field, None))
            continue
          relSect, _relOff = unhash(v)
          contributions.append((v, field,
                                v if relSect is not None else None))
      elif stype in ("F", "H"):
        for exp in suboperand.values:
          ev = exp[1] if isinstance(exp, (tuple, list)) \
              and len(exp) >= 2 else exp
          if isinstance(ev, (tuple, list)):
            ev = "".join(ev)
          v = parseSignedInt(str(ev))
          if v is None:
            fv = evalArithmeticExpression(exp, NO_LOCALS, stmt,
                                          symtab, currentHash())
            if fv is None:
              error(stmt,
                    f"Cannot evaluate {stype}-type constant")
              fv = 0
            # F/H take the numeric image; a relocatable F/H operand
            # (uncommon) contributes its displacement, no RLD here.
            v = int(_num(fv))
          contributions.append((v, field, None))
      elif stype == "B":
        # No explicit length -> the field width is the data's own width.
        s = suboperand.values[0]
        f = field if (isBits or width is not None) else len(s)
        contributions.append((int(s, 2) if s else 0, f, None))
      elif stype == "X":
        s = suboperand.values[0]
        f = field if (isBits or width is not None) else len(s) * 4
        contributions.append((int(s, 16) if s else 0, f, None))
      elif stype == "C":
        s = suboperand.values[0]
        ebc = s.encode("ebcdicvagc")
        f = field if (isBits or width is not None) else len(ebc) * 8
        value = 0
        for b in ebc:
          value = (value << 8) | b
        have = len(ebc) * 8
        if have < f:               # right-pad with EBCDIC blanks
          pad = f - have
          for _ in range(pad // 8):
            value = (value << 8) | 0x40
          value <<= pad - (pad // 8) * 8
        elif have > f:
          value &= (1 << f) - 1
        contributions.append((value, f, None))
      else:
        error(stmt,
              f"Unsupported bit-length DC/DS type {stype}")
        continue

      for _copy in range(dup):
        for (v, fwidth, relo) in contributions:
          byteAligned = (acc["nbits"] % 8 == 0) and (fwidth % 8 == 0)
          if operation == "DC" and relo is not None:
            relSect, relOff = unhash(v)
            combinedOffset = relOff + \
                sectionOffset(relSect)
            if byteAligned:
              rldSymbol = resolveCSect(combinedOffset, sects, relSect, exclude=sect)
              if passCount == 3:
                appendReloc(relocations, rldSymbol, sect,
                            startBytePos + acc["nbits"] // 8,
                            'Y' if fwidth == 16 else 'A')
              v = combinedOffset
            else:
              error(stmt,
                    "Relocatable symbol in sub-byte bit field "
                    "assembled as absolute (no RLD emitted)", 0)
              v = combinedOffset
          appendBits(v, fwidth)

    pad = (-acc["nbits"]) % 8
    acc["bits"] <<= pad
    acc["nbits"] += pad
    nbytes = acc["nbits"] // 8
    if operation == "DC":
      out = bytearray(nbytes)
      for i in range(nbytes):
        out[i] = (acc["bits"] >> (8 * (nbytes - 1 - i))) & 0xFF
      toMemory(out)
    else:
      toMemory(nbytes)
    
  # Evaluate a single suboperand of the operand of an instruction like 
  # RR, RS, SRS, SI, RI.  Returns a pair (err,value).  The 'err' is 
  # boolean and is True on error.  The value is an integer, or None if the
  # desired subfield isn't present.  The possible subfields are strings like
  # "R1", "R2", "D2", "X2", "B2", and so on.
  def evalInstructionSubfield(stmt, subfield, ast, symtab):
    if subfield not in ast:
      return False, None
    expression = ast[subfield]
    value = evalArithmeticExpression(expression, NO_LOCALS, stmt, symtab, \
                                     currentHash(), severity=0)
    if value == None:
      error(stmt, f"Could not evaluate {subfield} subfield", \
            severity=0)
      return True, None
    return False, value
    
  # For a D2(B2)/D2(X2,B2) operand written as just D2, pick a base register
  # from 'USING' and return (B2, adjusted D2) with the base subtracted, or
  # None,None if none fits.  'd2' is hashed; there is no upper limit on the
  # returned D2 -- the caller checks whether it fits.  This mirrors AS037F1's
  # IEUF8M DECOMP (same-section entries, smallest displacement, highest
  # register on ties -- DECOMP rejects only a strictly-higher displacement).
  def findB2D2(d2):
    if not isinstance(d2, Reloc):
      return None, (_num(d2) & 0xFFFFFF)
    return unUsing(using, d2, limit=4095)

  #-----------------------------------------------------------------------
  # Per-operation handlers.
  #
  # One nested function per instruction/pseudo-op family.  Each is invoked
  # from the per-line dispatch in the main pass loop below and shares that
  # loop's state through the enclosing scope (the current 'stmt',
  # 'operation', 'sect', 'using', the pass flags 'collect'/'asis'/'compile',
  # 'literalPoolNumber', and the memory helpers above).  A handler that needs
  # to *reassign* one of those enclosing locals declares it 'nonlocal'; one
  # that only reads it does not.  Where the original inline block did a
  # 'continue' to advance the main loop, the handler 'return's instead (inner
  # loops keep their own 'continue').

  def handleRR():
    # RR-format instructions (register-register).
    commonProcessing(2)
    if not compile:
      toMemory(2)
      return
    data = bytearray(2)
    ast = stmt.ast
    if ast != None:
      # Mnemonics with an implied R1 (see RR_IMPLIED_R1/BRANCH_ALIASES_R) take
      # only an R2 operand; a parsed R1 subfield means the source supplied
      # one, which is an error.
      impliedR1 = RR_IMPLIED_R1.get(operation, BRANCH_ALIASES_R.get(operation))
      if impliedR1 is not None:
        err = bool(ast.get("R1"))
        r1 = impliedR1
      else:
        err, r1 = evalInstructionSubfield(stmt, "R1", \
                                          ast, symtab)
      if not err and r1 >= 0 and r1 <= 7:
        err, r2 = evalInstructionSubfield(stmt, "R2", ast, symtab)
        lolim = 0
        uplim = 7
        if operation in ["LFXI", "LFLI"]:
          lolim = 0
          uplim = 15
          if operation == "LFXI":
            r2 += 2
            stmt.adr2 = r2
        if not err and r2 >= lolim and r2 <= uplim:
          if operation in BRANCH_ALIASES_R \
                  or operation in ("BR", "NOPR"):
            # Register-form branch: no descriptor of its own; encode
            # through BCR with the implied condition mask in r1.
            data[:] = CPU.encode("BCR", {"x": r1, "y": r2})
          else:
            # Encode from the GPC descriptor.  (LACR resolves through
            # CPU_ALIASES to LCR's descriptor.)
            data[:] = CPU.encode(operation, {"x": r1, "y": r2})
    toMemory(data)

  def handleRI():
    nonlocal operation
    commonProcessing(2)
    if not compile:
      toMemory(4)
      return
    data = bytearray(4)
    ast = stmt.ast
    if ast != None:
      err, r2 = evalInstructionSubfield(stmt, "R2", ast, symtab)
      if not err and r2 >= 0 and r2 <= 7:
        err, i1 = evalInstructionSubfield(stmt, "I1", ast, symtab)
        if not err:
          if i1 != None:
            stmt.adr2 = _num(i1) & 0xFFFF
          # Capture an EXTRN i1 before mutation: LHI R0,@0SYM (HAL/S
          # PASS2) needs a Y-type RLD so the linker fills the address.
          extrnI1 = (operation == "LHI" and i1 is not None
                     and i1 in rextrns)
          extrnI1Sym = rextrns[i1] if extrnI1 else None
          # Likewise an LHI whose immediate resolves into another CSECT
          # of this module (e.g. 'LHI R1,#DFPSMK') needs a Y-type RLD.
          # Do NOT zero i1: the linker's cross-section convention is
          # final = existing + (baseAddr - sectionAddr), so existing
          # must hold the in-module offset (mirrors the RS path).
          sectI1Sym = None
          if (operation == "LHI" and not extrnI1 and
                  i1 is not None):
            section, offset = unhash(i1)
            if section is not None and section in sects \
                    and sects[section].offset is not None:
              # Symbol values are rebased to the first CSECT's
              # address space, so resolve abs_hw (the absolute
              # halfword offset) back to its containing CSECT.
              abs_hw = offset + sectionOffset(section)
              rldSymbol = resolveCSect(abs_hw, sects, section)
              sectI1Sym = rldSymbol
          if operation == "SHI":
            i1 = -i1
            operation = "AHI"
          if operation == "LHI":
            # LHI is RI syntactically but is really an alias for LA
            # (RS, AM=0, B2=3): first halfword from LA's descriptor,
            # immediate i1 appended below as the displacement.
            data[0:2] = rs_hw1("LA", r2, 3, indexed=False)

          else:
            # RI first halfword from the descriptor; immediate i1
            # appended below.  (SHI was rewritten to AHI, LHI took
            # its own branch, so every op here is descriptor-typed.)
            data[0:2] = CPU.encode(operation, {"y": r2})
          if extrnI1:
            # Linker will fill in the displacement; emit zero
            # and add a Y-type relocation entry.
            i1 = 0
            if passCount == 3:
              emitDisplacementReloc(extrnI1Sym)
          elif sectI1Sym is not None:
            # Cross-section reference: leave i1 alone
            # (it already holds the in-module offset that
            # the linker's YCON apply formula expects);
            # just add the Y-type relocation entry.
            if passCount == 3:
              emitDisplacementReloc(sectI1Sym)
          i1 = _num(i1) & 0xFFFF
          data[2] = i1 >> 8
          data[3] = i1 & 0xFF
    toMemory(data)
    return
                

  def handleSI():
    commonProcessing(2)
    if not compile:
      toMemory(4)
      return
    data = bytearray(4)
    ast = stmt.ast
    if ast != None:
      err, d2 = evalInstructionSubfield(stmt, "D2", ast, symtab)
      if not err:
        if d2 != None:
          stmt.adr1 = _num(d2) & 0xFFFF
        err, b2 = evalInstructionSubfield(stmt, "B2", ast, symtab)
        if not err:
          if b2 == None:
            b2, d2 = unUsing(using, d2)
          if b2 != None and b2 >= 0 and b2 <= 3:
            err, i1 = evalInstructionSubfield(stmt, "I1", ast, symtab)
            i1 = _num(i1) & 0xFFFF
            stmt.adr2 = i1
            d2 = _num(d2) & 0b111111
            # SI first halfword from the descriptor; immediate i1
            # appended below.
            data[0:2] = CPU.encode(operation, {"d": d2, "b": b2})
            data[2] = i1 >> 8
            data[3] = i1 & 0xFF
          else:
            error(stmt, "Cannot identify base register")
    toMemory(data)
    return
                

  def handleMSC_BCE():
    # IOP (MSC/BCE) encoding, descriptor-driven via iop_instr
    # (validated by test_asm101_iop).  The IOP runs
    # inline in the CPU instruction stream, sharing this section's LOC,
    # symbol table, and relocation algebra.  Field model (see the
    # iop_instr / test_asm101_iop conventions):
    #   * the multi-bit value_fields take the comma-separated operands
    #     positionally, AFTER dropping any alias-forced field (the
    #     branch condition, which the mnemonic bakes in);
    #   * the 1-bit index field (named 'i' for long-format ops, 'm' for
    #     the BCE store-status #SSC/#SST) is set by a trailing "(1)";
    #   * field 'a' (long-format 18-bit) is an ABSOLUTE main-store
    #     address -- a relocatable term emits a fullword ACON RLD over
    #     the whole instruction (see the note at the emit site);
    #   * field 'd' of a memory/branch op (iop_instr.PC_RELATIVE) is a
    #     PC-RELATIVE displacement = target - (IC + 1) (the section term
    #     cancels for a same-section target, leaving a bare int);
    #   * every other field is the operand's evaluated value.
    # Instruction length is fixed by the descriptor (no operand-driven
    # short/long choice), so addresses never shift between passes.
    desc = iop_instr.IOP.descriptor(operation)
    if desc is None:
      error(stmt, f"Unimplemented IOP pseudo-op {operation}")
      return
    commonProcessing(2)
    base, forced = iop_instr.IOP.resolve(operation)
    fields = dict(forced)
    ast = stmt.ast
    ops = ast.get("ops", []) if isinstance(ast, dict) else []
    indexExp = ast.get("index") if isinstance(ast, dict) else None
    idxField = iop_instr.index_field(operation)
    if idxField is not None:
      idx = evalArithmeticExpression(
          indexExp, NO_LOCALS, stmt, symtab, currentHash(),
          severity=0) if indexExp is not None else None
      fields[idxField] = 1 if idx else 0
    icHalf = currentHash()      # this instruction's halfword address
    freeFields = [f for f in iop_instr.value_fields(operation)
                  if f not in forced]
    for fld, exp in zip(freeFields, ops):
      v = evalArithmeticExpression(exp, NO_LOCALS, stmt, symtab,
                                   currentHash(), severity=0)
      if v is None:
        v = 0
      width = desc.fields[fld][2]
      mask = (1 << width) - 1
      if fld == "a":
        # The 18-bit 'a' field spans both halfwords: a[17:16] are the low
        # bits of hw1 and a[15:0] fill hw2.  The reloc is a fullword ACON
        # add over the whole 32-bit instruction, so the link-time base add
        # carries out of hw2 into hw1's low bits (a 2-byte hw2 reloc
        # would drop address bit 16).  Sub-64K targets are unaffected:
        # the carry never reaches hw1.
        relSect, relOff = unhash(v)
        if relSect is not None and passCount == 3:
          combinedOffset = relOff + sectionOffset(relSect)
          rldSymbol = resolveCSect(combinedOffset, sects,
                                   relSect, exclude=sect)
          appendReloc(relocations, rldSymbol, sect, sects[sect].pos1,
                      type='A')
          v = combinedOffset
        elif relSect is None and relOff is None and passCount >= 3:
          error(stmt, "Complex relocatable expression not "
                "supported in an IOP address", severity=0)
        fields[fld] = _num(v) & mask
      elif fld == "d" and base in iop_instr.PC_RELATIVE:
        d = v - (icHalf + 1)
        if isinstance(d, Reloc):
          error(stmt, "PC-relative IOP target is not in "
                "the current section", severity=0)
          d = 0
        fields[fld] = _num(d) & mask
      else:
        fields[fld] = _num(v) & mask
    try:
      data = bytearray(iop_instr.IOP.encode(base, fields))
    except Exception as exc:
      error(stmt, f"IOP encode failed: {exc}")
      data = bytearray(desc.nbits // 8)
    toMemory(data)
    return

  def handleDcDs():
    nonlocal name
    nonlocal dcBufferPtr
    ast = stmt.ast
    if ast == None:
      error(stmt, f"Cannot parse {operation} operand")
      return
    flattened = list(ast)   # copy: stmt.ast is shared with the parse cache
    dcBufferPtr = 0
    # 'flattened' is one DcSuboperand per suboperand (.dup/.type/.length/
    # .values/.hexstr/.zexpr/.flags/.scale); manual GC28-6514-8 "DC", pdf p. 46.

    # Record the L' length attribute of a DS/DC-defined symbol: the HALFWORD
    # footprint of ONE element of the first suboperand (the AP-101 location
    # counter is in halfwords and L' is mixed into halfword offset
    # arithmetic: L' over a 'DS F' is 2, not 4 bytes).  Only
    # the EQU-with-length form was captured before (at macro-expansion time), so
    # 'L'symbol' on a DS/DC symbol previously errored.  Additive: an unrecorded
    # L' always errored, so this never changes a symbol that already resolved.
    if name != "" and flattened:
      _lv, _isb, _lerr = parseDcLength(flattened[0].length)
      _bytes = None
      if not _lerr:
        if _lv is not None:
          _bytes = max(1, (_lv + 7) // 8) if _isb else _lv
        else:
          _t = flattened[0].type
          _dct = DCTYPES.get(_t)
          _bytes = _dct.nbytes if _dct is not None else None
          if _bytes is None:
            _vals = flattened[0].values
            if _t == "C":
              _bytes = max(1, len(_vals[0])) if _vals else 1
            elif _t == "X":
              _bytes = max(1, (len(_vals[0]) + 1) // 2) if _vals else 1
            elif _t == "B":
              _bytes = max(1, (len(_vals[0]) + 7) // 8) if _vals else 1
            else:
              _bytes = 1
      if _bytes is not None:
        # bytes -> halfwords (round up): the unit L' is consumed in.
        asmContext.symbolAttributes.setdefault(name, {})["length"] = \
            max(1, (_bytes + 1) // 2)

    # If any suboperand has a sub-byte bit-length ("L.n") modifier, the
    # whole DC/DS goes through the contiguous bit-stream packer instead of
    # the byte-aligned per-suboperand path below.
    usesBitLength = False
    for so in flattened:
      _v, _isb, _e = parseDcLength(so.length)
      if _isb:
        usesBitLength = True
        break
    if usesBitLength:
      packBitLengthDC(stmt, operation, flattened)
      return

    # Carry the DC's label on the FIRST suboperand only.  Each type handler
    # calls commonProcessing, which assigns the label at the current
    # location, so otherwise a multi-suboperand DC (e.g. 'Y(SYM),X'2'')
    # would land its label on the last suboperand, at the wrong address.
    dcLabel = name
    for subIndex, suboperand in enumerate(flattened):
      name = dcLabel if subIndex == 0 else ""
      # The buffer restarts at every suboperand: each type handler emits
      # dcBuffer[:dcBufferPtr] after packing, so a statement-lifetime
      # buffer re-emitted every prior suboperand ('DC Y(X),H'130'' laid
      # down Y,Y,H -- FCMCBLKS FCMHTABL, 6 spurious halfwords) and skewed
      # emitAddressConstant's pos1+dcBufferPtr RLD positions.
      dcBufferPtr = 0
      if suboperand.dup is None:
        duplicationFactor = 1
      else:
        duplicationFactor = \
            evalArithmeticExpression(suboperand.dup, NO_LOCALS, \
                                     stmt, symtab, currentHash())
        if duplicationFactor == None:
          error(stmt, "Could not evaluate duplication factor")
          continue
      suboperandType = suboperand.type

      # Z-type (ZCON), DC Z(symbol,,flags): a 4-byte fullword-aligned
      # external reference with relocation -- a different layout from the
      # other types, so handled before the shared length/alignment logic.
      if suboperandType == "Z":
        commonProcessing(4)
        if operation == "DC":
          # The reloc target is an arith expr: a bare Sym, or a simply-
          # relocatable Sym +/- a constant ADDEND (e.g. 'Z(,FIOBRU+4,0)',
          # 'Z(FPMSVCL+2,...)').  Split it into (symbol name, addend); the
          # addend goes into the HW0 address field below, and the linker ADDS
          # the resolved symbol address to it (addrcon.AddrCon.apply is additive).
          symbolName, addend = zRelocTarget(suboperand.zexpr)
          if addend is None:
            addend = 0
          # The relocated address may be the CODE subfield ('Z(sym...)',
          # ZCON/code RLD 0x04) or the DATA subfield ('Z(,sym...)',
          # ZCON/data 0x50, where the linker also patches DSR).
          zType = 'ZD' if suboperand.zdata else 'Z'
          zexpr = suboperand.zexpr
          if symbolName is None and zexpr is not None and passCount >= 3:
            error(stmt, "Unsupported Z-con reloc target "
                  "(expected a symbol +/- a constant addend)")
          flags = 0
          if suboperand.flags is not None:
            flags = evalArithmeticExpression(suboperand.flags, NO_LOCALS,
                                             stmt, symtab,
                                             currentHash())
            if flags is None:
              flags = 0

          hw0 = addend
          if symbolName:
            sym = symtab.get(symbolName)
            if sym is None or sym.type == "EXTERNAL":
              # Genuinely external target: ER ESD + RLD against it; the
              # linker adds the resolved absolute address to the addend.
              registerExtrn(symbolName, stmt.n)
              if passCount == 3:
                pos1 = sects[sect].pos1
                # The ZCON flags byte goes into the data image
                # below, not the RLD entry; the writer derives
                # the RLD flag from the 'Z' type.
                appendReloc(relocations, symbolName, sect, pos1, zType)
            elif passCount >= 3:
              # Target defined in this assembly: mirror emitAddressConstant.
              # Fold the assembled halfword address into HW0 and relocate
              # against the containing CSECT (the linker adds the section's
              # relocation factor) -- NOT an ER, which would make the linker
              # demand an export nothing provides.
              v = evalArithmeticExpression(zexpr, NO_LOCALS, stmt, symtab,
                                           currentHash())
              relSect, relOff = unhash(v)
              if relSect is not None:
                # 'zexpr' already includes the addend.
                hw0 = relOff + sectionOffset(relSect)
                if passCount == 3:
                  pos1 = sects[sect].pos1
                  rldSymbol = resolveCSect(hw0, sects, relSect, exclude=sect)
                  appendReloc(relocations, rldSymbol, sect, pos1, zType)
              elif relOff is not None:
                # Absolute (EQU) target: no RLD; a linked ZCON HW0 is always
                # sector-encoded, so set bit 15 the way the linker would.
                hw0 = 0x8000 | (relOff & 0x7FFF)
              else:
                error(stmt, "Complex relocatable expression not "
                      "supported in Z constant")

          # See 'ZCon.prelink' for the 4-byte ZCON layout; 'flags' is the
          # source flags operand.
          dcBuffer[dcBufferPtr:dcBufferPtr + 4] = \
              ZCon.prelink(hw0, flags).to_bytes()
          dcBufferPtr += 4
          toMemory(dcBuffer[:dcBufferPtr])
        else:
          # DS Z - just reserve 4 bytes
          toMemory(duplicationFactor * 4)
        continue
                    
      # The "Ln" length modifier (e.g. CL4, XL2) is a ('L', digits) tuple,
      # not an arithmetic expression, so it goes through parseDcLength, not
      # evalArithmeticExpression.  The sub-byte L.n form already went to
      # packBitLengthDC above, so isBits is always False here.
      _lenVal, _lenIsBits, _lenErr = \
          parseDcLength(suboperand.length)
      if _lenErr:
        error(stmt, "Could not evaluate length modifier")
        continue
      lengthModifier = _lenVal
      astValue = suboperand.values
      if suboperandType == "C":
        commonProcessing(1)
        # Body still carries IBM's doubled escapes: '' -> ' and && -> &.
        try:
          rawChars = astValue[0]
        except Exception:
          error(stmt, "Cannot parse C value")
          continue
        cText = rawChars.replace("''", "'").replace("&&", "&")
        # Implicit length is the number of characters; an explicit
        # length modifier fixes the field width, truncating on the
        # right if longer and padding with EBCDIC blanks (0x40) if
        # shorter.
        if lengthModifier is None:
          fieldLen = len(cText)
        else:
          fieldLen = lengthModifier
        field = bytearray(
            cText[:fieldLen].encode("ebcdicvagc").ljust(fieldLen, b"\x40"))
        if operation == "DC":
          cBytes = bytearray()
          for _dup in range(duplicationFactor):
            cBytes += field
          toMemory(cBytes)
        else:
          toMemory(duplicationFactor * fieldLen)
      elif suboperandType == "X":
        commonProcessing(1)
        # Adjust the string to have an even number of digits,
        # 0-padded or truncated to the length modifier, if any.
        try:
          # Index the current suboperand, not flattened[0]: a DC can
          # mix types (e.g. "DC Y(SYM),X'11'").
          hexString = suboperand.values[0]
          if lengthModifier == None:
            # With no explicit length, an X constant occupies
            # ceil(digits/2) bytes rounded UP to a halfword (AP-101
            # stores hex constants halfword-wide, right-justified):
            # X'0' -> 0000, X'F' -> 000F, X'2' -> 0002.
            byteLen = (len(hexString) + 1) // 2
            if byteLen % 2:
              byteLen += 1
            count = byteLen * 2
          else:
            count = lengthModifier * 2
          while len(hexString) < count:
            hexString = "0" + hexString
          hexString = hexString[-count:]
        except:
          error(stmt, "Cannot parse X value")
          continue
        # Deal with memory.  Like every other type, an X-constant honors
        # the duplication factor (e.g. '(N)X'FFFF'' -> N copies); replicate
        # the assembled bytes / reserved width 'duplicationFactor' times.
        if operation == "DC":
          # A non-hex character here is a real deck error (or an
          # unsubstituted &VAR that leaked through macro expansion);
          # diagnose it rather than crashing in int().
          if not all(c in "0123456789ABCDEFabcdef" for c in hexString):
            error(stmt, f"Invalid hexadecimal constant X'{hexString}'")
            continue
          bytes = bytearray()
          for i in range(0, count, 2):
            b = hexString[i:i+2]
            bytes.append(int(b, 16))
          toMemory(bytes * duplicationFactor)
        else:
          toMemory(duplicationFactor * (count // 2))
      elif suboperandType == "B":
        commonProcessing(1)
        # B-type (binary) constant.  Bits pack MSB-first into
        # ceil(bits/8) bytes,
        # leading byte zero-padded (IBM Assembler-F IEUF8D).  An implicit
        # length rounds UP to a halfword (as the X branch does) to avoid
        # an odd-byte location counter; an explicit BLn is honored exactly
        # (left-pad/left-truncate).  We haven't see any sub-halfword 
        # implicit B-cons; halfword rounding is chosen for consistency.
        try:
          bitString = suboperand.values[0]
          if lengthModifier == None:
            fieldLen = max(1, (len(bitString) + 7) // 8)
            if fieldLen % 2:
              fieldLen += 1
          else:
            fieldLen = lengthModifier
          nbits = fieldLen * 8
          bitString = bitString.rjust(nbits, "0")[-nbits:]
        except:
          error(stmt, "Cannot parse B value")
          continue
        if operation == "DC":
          bbytes = bytearray()
          for i in range(0, nbits, 8):
            bbytes.append(int(bitString[i:i+8], 2))
          toMemory(bbytes * duplicationFactor)
        else:
          toMemory(duplicationFactor * fieldLen)
      elif suboperandType in ["F", "H"]:
        dct = DCTYPES[suboperandType]
        length = dct.nbytes
        multiplier = dct.fullScale
        mask = (1 << (8 * length)) - 1
        if lengthModifier != None:
          commonProcessing(1)
        else:
          commonProcessing(dct.align)
        if operation == "DC":
          # A plain or singly-negated integer is stored as-is;
          # anything else is a fixed-point fraction scaled by the
          # type's full-scale multiplier (and clamped).
          # A comma-separated nominal-value list (DC H'0,1,160,37,0,0,0')
          # emits one field PER value, exactly as the E/D branch below
          # does.
          for fstr in suboperand.values:
            isInt = fstr.isdigit() or \
                    (fstr[:1] == '-' and fstr[1:].isdigit())
            expInt = None if isInt else decimalExponentInt(fstr)
            if suboperand.scale is not None:
              # Fixed-point scale modifier (e.g. FS-19, FS20): multiply by
              # 2**scale (the binary point sits 'scale' bits from the right,
              # GC28-6514 "scale modifier"); a result of magnitude < 1 is a
              # fraction of full scale.  Mirrors the =F/=H literal scale path
              # (round, not the unscaled float branch's truncation).
              sv = (int(fstr) if isInt else float(fstr)) \
                  * pow(2.0, -suboperand.scale)
              if -1.0 < sv < 1.0:
                sv *= multiplier
                if sv >= multiplier:
                  sv = multiplier - 1
                elif sv <= -multiplier:
                  sv = -multiplier + 1
              v = round(sv) & mask
            elif isInt:
              v = int(fstr) & mask
            elif expInt is not None:
              v = expInt & mask
            else:
              v = float(fstr) * multiplier
              if v >= multiplier:
                v = multiplier - 1
              elif v <= -multiplier:
                v = -multiplier + 1
              v = int(v) & mask
            dcBufferPtr = packBE(dcBuffer, dcBufferPtr, v, length)
          # Replicate the assembled value(s) 'duplicationFactor' times.
          toMemory(dcBuffer[:dcBufferPtr] * duplicationFactor)
          continue
        toMemory(duplicationFactor * length)
      elif suboperandType in ["E", "D"]:
        fpLength = DCTYPES[suboperandType].nbytes
        commonProcessing(4)  # Even doublewords are aligned to fullword.
        length = fpLength
        if operation == "DC":
          length = 0
          for fstr in suboperand.values:
            # We now have to convert the 'value' to an
            # IBM hexadecimal float, of either single
            # precision ('fpLength==4') or double
            # precision ('fpLength==8').  An E constant ROUNDS at the
            # 24-bit single-precision fraction (the double conversion's
            # msw would instead truncate the last bit).
            msw, lsw = toFloatIBM(fstr, single=(fpLength == 4))
            dcBufferPtr = packBE(dcBuffer, dcBufferPtr, msw, 4)
            if fpLength == 8:
              dcBufferPtr = packBE(dcBuffer, dcBufferPtr, lsw, 4)
          # Replicate the assembled value(s) 'duplicationFactor' times.
          toMemory(dcBuffer[:dcBufferPtr] * duplicationFactor)
          continue
        toMemory(duplicationFactor * length)
      elif suboperandType == "A":
        if lengthModifier != None:
          commonProcessing(1)
        else:
          commonProcessing(4)
        if operation == "DC":
          if suboperand.hexstr is not None:
            # Self-defining hex form, DC A'1F'.
            lsw = int(suboperand.hexstr, 16)
            dcBufferPtr = packBE(dcBuffer, dcBufferPtr, lsw, 4)
          else:
            # Address form, DC A(expr): a 4-byte adcon with a 4-byte
            # RLD (type 'A' -> AddrCon flag 0x1C) for any relocatable
            # term, so A(EXTRN)/A(label) can be linked.
            emitAddressConstant(suboperand, 4, 'A', "A-type")
          toMemory(dcBuffer[:dcBufferPtr] * duplicationFactor)
          continue
        toMemory(duplicationFactor * 4)
      elif suboperandType == "Y":
        if lengthModifier != None:
          commonProcessing(1)
        else:
          commonProcessing(2)
        if operation == "DC":
          # Halfword address constant: a 2-byte RLD (type 'Y' ->
          # AddrCon flag 0x00) for any relocatable term.
          emitAddressConstant(suboperand, 2, 'Y', "Y-type")
          # Replicate the assembled value(s) 'duplicationFactor' times.
          toMemory(dcBuffer[:dcBufferPtr] * duplicationFactor)
          continue
        toMemory(duplicationFactor * 2)
      else:
        error(stmt,
              f"Unsupported DC/DS type {suboperandType}")
        continue
    return
            

  def handleSrsOrRs():
    nonlocal operation
    commonProcessing(2)
    if collect and not asis:
      dataSize = 4
    else:
      dataSize = stmt.length
    if operation in ARGS_SRS_ONLY:
      dataSize = 2
    ast = stmt.ast
    literalAttributes = None
    if ast == None:
      error(stmt, "Could not parse operands of " + operation)
      return
    if "L2" in ast:
      literalAttributes = evalLiteralAttributes(stmt, ast, symtab)
      if literalAttributes == None:
        return
      if collect and not asis:
        pool = literalPools[-1]
        if literalIndex(pool, literalAttributes) < 0:
          pool.add(literalAttributes)
      else:
        pool = literalPools[literalPoolNumber]
        idx = literalIndex(pool, literalAttributes)
        if idx < 0:
          error(stmt,
                f"Literal not in literal pool: {literalAttributes.operand}")
          return
        # Refresh the slot's value/assembled bytes: a =Y(expr)
        # over forward labels resolves across passes, and the
        # final-pass memory write reads the slot's 'assembled'.
        # The operand key and length (layout) are unchanged.
        pool.literals[idx].value = literalAttributes.value
        pool.literals[idx].assembled = literalAttributes.assembled
    if not compile:
      toMemory(dataSize)
      return
    data = bytearray(dataSize)
    # 'ast' is non-None here (we return above if it failed to parse).
    # The R1/D2/B2/X2 evaluations below are guard clauses: any failure
    # falls through to the tail 'toMemory(data)' with the default
    # (zero) 'data', exactly as the old nested ladder did.
    err, r1 = evalInstructionSubfield(stmt, "R1", ast, symtab)
    if operation in BRANCH_AT_POUND_VARIANTS:
      # A condition-code branch ALIAS in @/# RS form (B@/BZ@/BNZ@/B#/...): the
      # condition mask is implicit in the mnemonic (like BZ -> mask 4), and the
      # @/# form has no SRS short, so encode exactly like 'BC@ mask,...'.  Supply
      # the mask as R1 and rewrite to the BC descriptor mnemonic, KEEPING the
      # @/# suffix so the indirect/immediate ('ia'/'i') bits are still applied.
      stem = operation.rstrip("@#")
      r1 = BRANCH_ALIASES[stem]
      operation = "BC" + operation[len(stem):]
    elif r1 == None:
      # R1 syntactically omitted -> use an implied R1: a branch alias
      # supplies its condition MASK; a no-R1 SRS/RS op bakes R1 into
      # fixed opcode bits, which implied_r1 reads off descriptor bits
      # 5-7.  Stays None for an op that takes an explicit R1.
      if operation in BRANCH_ALIASES:
        r1 = BRANCH_ALIASES[operation]
      else:
        r1 = implied_r1(operation)
    if r1 == None:
      toMemory(data)
      return
    if literalAttributes != None:
      # 2nd operand is a literal (L2 found in 'ast' above).
      pool = literalPools[literalPoolNumber]
      err = False
      d2 = (pool.offset
            + pool.offsets[literalIndex(pool, literalAttributes)]).hw
    else:
      err, d2 = evalInstructionSubfield(stmt, "D2", ast, symtab)
    originalD2 = d2
    extrnD2 = (d2 in rextrns)
    if err or d2 == None:
      toMemory(data)
      return
    stmt.adr1 = _num(d2) & 0xFFFF
    err, b2 = evalInstructionSubfield(stmt, "B2", ast, symtab)
    if err:
      toMemory(data)
      return
    err, x2 = evalInstructionSubfield(stmt, "X2", ast, symtab)
    if err:
      toMemory(data)
      return
    # 'forceRS' (operation can only be encoded
    # as RS, not SRS) determines whether b2 in
    # [4..7] is legal as-is.  RS form has a
    # 3-bit B2 field (R0-R7); SRS has only 2
    # bits (R0-R3).  Compute early so the
    # atStar b2>3 heuristic below can skip the
    # spurious b2->x2 swap for RS-only ops.
    forceRS = (operation in ARGS_RS_ONLY) \
        or stmt.longFormat
    atStar = "@" in operation or "#" in operation \
        or (b2 != None and b2 > 3 and not forceRS)
    bareParen = b2 != None and x2 == None and not ast.get("B2ONLY")
    if bareParen and isinstance(d2, Reloc) and not extrnD2 \
            and operation != "LA":
      # LA is exempt: its paren register is the BASE even over a
      # relocatable displacement (the AM=0 based form with the absolute
      # assembled address, same as the absolute-displacement LA rule).
      # A bare paren register over a RELOCATABLE displacement is an INDEX
      # -- ANY register, not just the @/#-or-R4-R7 heuristic below: the
      # base comes from the covering USING.  An ABSOLUTE displacement
      # keeps the register as base -- the split is
      # relocatable-vs-absolute.
      b, d = unUsing(using, d2)
      if b != None:
        x2 = b2
        b2 = b
        d2 = d
      elif not atStar and unhash(d2)[0] == sect:
        # Current-section relocatable, no covering USING: the reference is
        # self-relative (current-section fallback), the register an index.
        # A $-forced AM=0 encoding has no index field -- the register is
        # dropped but the displacement relocation KEPT (Y RLD over the
        # 16-bit field); treating the register as a base would suppress
        # that RLD.
        if not stmt.longFormat:
          x2 = b2
        b2 = None
      elif atStar:
        # @/# (or R4-R7) with no covering USING: keep the legacy form
        # (register in X2, and -- unchanged -- still in B2).
        x2 = b2
    elif atStar and bareParen:
      # A bare paren register on an @/# op is an INDEX (base comes from
      # USING) -- but the COMMA form 'expr(,Rn)' names the BASE with the
      # index slot explicitly empty, and must keep it empty (x2=0).
      if ("@" in operation or "#" in operation) and b2 <= 3 \
              and not isinstance(d2, Reloc):
        # ... and over a NUMERIC (absolute) displacement the register is
        # the BASE alone: the @/# indirect form must not duplicate it
        # into hw2's X2 field.
        pass
      else:
        x2 = b2
        b, d = unUsing(using, d2)
        if b != None:
          b2 = b
          d2 = d
    done = False
    forceAM0 = False
    if operation in ["BC", "BCT"]:
      # PC-relative displacement; a bare int only
      # when d2 is in the current section (its
      # term cancels currentHash()'s), which is
      # the precondition for any SRS branch form.
      d = d2 - (currentHash() + 1)
      # Short SRS backward form (BCB/BCTB), unless '$'
      # forces long format.  Gate on longFormat, NOT
      # forceRS: BCT is in ARGS_RS_ONLY (forceRS always
      # True) yet still has a backward SRS form (BCTB).
      if not stmt.longFormat \
              and isinstance(d, int) \
              and fitsBackwardSrsBranch(d):
        d = (-d & 0b111111)
        data = generateSRS(stmt, \
                           operation+"B", \
                           r1, d, \
                           {"BC":0b10,
                            "BCT":0b11}[operation])
        done = True
      elif not stmt.longFormat \
              and operation == "BC" \
              and isinstance(d, int) \
              and fitsForwardSrsBranch(d, stmt):
        # Short SRS forward form (BCF); only BC has
        # one (BCT has no forward SRS form and falls
        # to the RS path).  Uses the PC-relative d
        # from the original hashed d2 -- computed
        # BEFORE findB2D2 rewrites d2 to a base-
        # relative value, which under a covering
        # USING (e.g. 'USING *,R12') would otherwise
        # be a meaningless SRS displacement.
        d = d & 0b111111
        data = generateSRS(stmt, "BCF",
                           r1, d, 0b00)
        done = True
    elif operation in BRANCH_ALIASES or operation == "BVC":
      # BVC is the explicit-mask overflow/carry
      # branch (BVC M1,D2); its r1 is the parsed
      # mask.  BOV/BOC are its implied-mask
      # extended mnemonics (r1 from BRANCH_ALIASES).
      # All three encode as BVCF forward; the
      # condition-code aliases (BO/BNO/BNC/...)
      # encode as BCF -- except BNC, which is
      # itself an overflow/carry branch.
      ovflCarry = operation in ("BNC", "BOV",
                                "BOC", "BVC")
      d = d2 - (stmt.pos1.hw + symtab[sect].value + 1)
      # A '$'-forced long branch skips the SRS
      # short forms and is coded as RS (BC).
      if not forceRS and isinstance(d, int) \
              and fitsForwardSrsBranch(d, stmt):
        d = d & 0b111111
        if ovflCarry:
          o = "BVCF"
          b = 0b01
        else:
          o = "BCF"
          b = 0b00
        data = generateSRS(stmt, o, r1, d, b)
        done = True
      elif not forceRS and isinstance(d, int) \
              and fitsBackwardSrsBranch(d):
        # Backward: BCB is the condition-code
        # form.  There is no SRS backward
        # overflow/carry form, so a backward
        # BVC/BOV/BOC would need the RS form
        # (BVC) -- not exercised by the deck;
        # fail loud rather than silently emit
        # the wrong (condition-code) family.
        if ovflCarry and operation != "BNC":
          error(stmt,
                "Backward overflow/carry "
                f"branch ({operation}) not implemented")
        d = (-d & 0b111111)
        data = generateSRS(stmt, "BCB", r1, d, 0b10)
        done = True
      elif ovflCarry and operation != "BNC":
        # Out-of-SRS-range overflow/carry
        # branch needs the RS form (BVC), not
        # BC; not exercised -- fail loud.
        error(stmt,
              "Out-of-range overflow/carry "
              f"branch ({operation}) not implemented")
        done = True
      else:
        operation = "BC"
        forceRS = True
    isConstant = False
    specifiedB2 = (b2 != None)
    # A symbolic displacement is PC/self-relative ONLY when it has no real base
    # register: the no-base default (ib2=3) or the current-section fallback.  An
    # EXPLICIT base (source 'SYM(R3)') or a USING-resolved base -- even R3 --
    # carries an ABSOLUTE base displacement, so it must NOT have the IC
    # subtracted.  Default False here; set True at the two self-relative sites.
    selfRelB2 = False
    if not done and b2 == None:
      # Recall that 'findB2D2' returns
      #    None,constantValue        or
      #    B2,unhashedValue          or
      #    None,None                 no match
      b2,newd2 = findB2D2(d2)
      if b2 == None:
        if newd2 == None:
          # Bare int only when d2 is in the
          # current section; otherwise it
          # stays relocatable -> not a B2=3
          # current-section displacement.
          newd2 = d2 - symtab[sect].value
          # 'used' is mid-rebuild during a compile pass (it resets with
          # pos1), so a FORWARD reference ranges against the previous
          # pass's 'usedPrev' estimate as well.
          if isinstance(newd2, int) \
                  and newd2 >= 0 and newd2 < 4096 \
                  and newd2 < max(sects[sect].used.hw,
                                  sects[sect].usedPrev.hw):
            b2 = 3
            d2 = newd2
            selfRelB2 = True       # current-section, no base -> PC-relative
          else:
            section,offset = unhash(d2)
            if section != None:
              forceAM0 = True
            else:
              error(stmt, "Cannot determine B2(D2)")
              done = True
        else:
          isConstant = True
          forceRS = True
      else:
        d2 = newd2
    if b2 != None and (b2 < 0 or b2 > 3) and operation not in SHIFT_OPERATIONS:
      if x2 == None and b2 >= 4 and b2 <= 7 and not forceRS:
        x2 = b2
        b2 = None
      elif b2 >= 0 and b2 <= 7 and (forceRS or x2 != None):
        # RS form has a 3-bit B2 field
        # (R0-R7); leave b2 alone and let
        # the RS encoding paths below
        # consume it.  Triggered for
        # forceRS ops (e.g. LPS X'48'(R4))
        # and for ordinary RS addressing
        # with explicit X2 and B2 in
        # [4..7] (e.g. STH R7,18(R1,R4)).
        pass
      else:
        error(stmt, "B2 out of range")
        done = True
    # bit0 = 1 for an op with no SRS short form; feeds the forceAM0 heuristic.
    rsBit0, _rsBit9 = rs_form_bits(operation)
    # 'forceAM0' is purely empirical.  A '$'-forced long instruction takes the
    # RS AM=0 (extended, 16-bit) form -- for a branch this is the absolute
    # 16-bit target (+ RLD); the AM=1 PC-relative form is for the
    # auto-selected (non-$) long.
    forceAM0 = forceAM0 or stmt.longFormat or ( rsBit0 \
                and b2 not in [3, None] and x2 == None)
    if extrnD2:
      forceAM0 = True
    if not done:
      # Precompute the values the SRS / RS AM=1 /
      # RS AM=0 form-selection below needs.
      unhashedValue = _num(d2) & 0xFFFFFF
      ic = sects[sect].pos1.hw
      icSRS = ic + 1
      icRS = ic + 2
      dUnitizer = srs_d2_units(operation)
      if "L2" in ast and dUnitizer == 2 and \
              b2 in [None, 3]:
        # A literal isn't known to be an integral
        # number of fullwords from here, so force RS.
        forceRS = True
        forceAM0 = True
      # D2 is absent for the literal (L2) form; a
      # bare integer displacement (e.g. the 0 in
      # 0(R2,3)) takes the absolute encodings below.
      d2IsNumber = isNumberD2(ast)
      if d2IsNumber:
        dSRSa = unhashedValue
        dRSAM1 = unhashedValue
      else:
        dRSAM1 = unhashedValue - icRS
        # The SRS short form's displacement is PC-relative ONLY for a self-
        # relative reference (no base register, or the current-section
        # fallback).  Over a real base -- an explicit 'SYM(R3)' or a USING-
        # resolved base, even R3 -- the value is already the base displacement,
        # so it must NOT have the IC subtracted (else a 'USING TBCEBUF,R3'
        # reference mis-encodes as PC-relative).
        if b2 is not None and not selfRelB2:
          dSRSa = unhashedValue
        else:
          dSRSa = unhashedValue - icSRS
      dSRS = (dSRSa + dUnitizer - 1) // dUnitizer
      forbiddenSRS = (dSRSa % dUnitizer) != 0
      uUnhashedValue = (dUnitizer - 1 + unhashedValue) // dUnitizer
      ia = "@" in operation
      i =  "#" in operation
      if ia or i:
        # Indirect ('@') and indexed ('#') addressing
        # exist ONLY in RS AM=1 (generateRS1 carries
        # the ia/i bits; AM=0 has none), so these must
        # encode AM=1 even if a heuristic set forceAM0
        # (e.g. the bare-operand form 'LDM@# R1,SYM').
        forceAM0 = False
      if b2 == None:
        ib2 = 3
        selfRelB2 = True           # no base register -> PC-relative
      else:
        ib2 = b2
      if ib2 == 3 and selfRelB2:
        # PC-relative d only for a TRUE self-relative reference (no base
        # register anywhere).  R3 CODED as a base -- explicit 'expr(R3)' /
        # '(X,R3)' or USING-resolved -- carries the absolute displacement,
        # never a PC-relativized one (whose EA is correct only by the
        # accident R3=0).
        d = dSRS
        d1 = dRSAM1
      else:
        d = uUnhashedValue
        d1 = unhashedValue
      if operation in SHIFT_OPERATIONS:
        if b2 == None:
          d = _num(d2)
        else:
          # Register-designated count: bits 10-15 of R(D-56) (POO p.78).
          d = SHIFT_REG_COUNT_BASE + b2
        # b2 is a don't-care for shifts: the shift-TYPE bits are FIXED in
        # the descriptor (generateSRS encodes {x, d}, no 'b' field).
        data = generateSRS(stmt, operation, r1, d, 0)
        stmt.adr1 = None
        stmt.adr2 = _num(d2) & 0x3F
      elif operation == "LA" and \
              x2 == None and b2 != None \
              and isinstance(d2, int) \
              and d2 > -2048 and d2 < 2048:
        if d2 >= 0 and d2 < SRS_CEILING and len(data) == 2:
          data = generateSRS(stmt, operation, r1, d2, ib2)
        elif not selfRelB2:
          # A real (USING-resolved or explicit) base, R3 included: LA
          # takes the AM=0 based form with the ABSOLUTE displacement,
          # not a PC-relativized form (correct EA only by coincidence of
          # layout).  selfRelB2 alone marks the PC-relative sentinel, so
          # no ib2 test.  The PC-relative branches below are for the
          # true self-relative/absolute-target cases.
          data = generateRS0(stmt, operation, r1, d2 & 0xFFFF, ib2)
        elif d2 < icRS:
          # RS AM=1 with the I bit set, so d2 is
          # subtracted from the updated IC.
          data = generateRS1(stmt, operation, 0, 1, r1, icRS - d2, 0, 3)
        else: # d2 >= ic + 2
          data = generateRS1(stmt, operation, 0, 0, r1, d2 - icRS, 0, 3)
      elif len(data) == 2  or \
             (not (ib2 == 3 and \
                   operation in FP_OPERATIONS_SP) and \
              not forceRS and x2 == None and \
              (specifiedB2 or ib2 == 3) and \
              fitsForwardSrsBranch(d, stmt) and \
              not forbiddenSRS and \
              operation in BRANCH_ALIASES):
        # Is SRS.
        if operation in ["BCB", "BCTB"]: # Backward displacement
          d = -d
        if operation in ["BC", "BCF"]:
          operation = "BCF"
          ib2 = 0b00
        elif operation == "BVC":
          operation = "BVCF"
          ib2 = 0b01
        elif operation == "BCB":
          ib2 = 0b10
        elif operation == "BCTB":
          ib2 = 0b11
        if d >= SRS_CEILING:
          error(stmt, "SRS displacement out of range")
        data = generateSRS(stmt, operation, r1, d, ib2)
      elif isConstant and literalAttributes == None:
        data = generateRS0(stmt, operation, r1, _num(d2) & 0xFFFF, 3)
      elif d2IsNumber and b2 != None and x2 == None and not i and not ia:
        data = generateRS0(stmt, operation, r1, _num(d2) & 0xFFFF, b2)
      elif literalAttributes != None:
        pool = literalPools[literalPoolNumber]
        d1 = (pool.offset
              + pool.offsets[literalIndex(pool, literalAttributes)]).hw \
             - icRS
        data = generateRS1(stmt, operation, 0, 0, r1, d1, 0, 3)
      elif extrnD2 and operation in ["BC", "BIX", "BAL"] and \
              x2 in [None, 0]:
        # Branch/BAL to an EXTRN symbol
        # (e.g. 'BAL R7,EOUT'). d1 is computed
        # from the EXTRN hashcode, so the
        # range checks below would be junk;
        # always emit RS AM=0 with zero
        # displacement and a Y-type RLD.
        data = generateRS0(stmt, operation, r1, 0, 3)
        if passCount == 3:
          emitDisplacementReloc(rextrns[originalD2])
      elif operation in ["BC", "BIX", "BAL", "BCT"] and x2 in [None, 0] and \
              d1 > -2048 and d1 < 0 and not forceAM0:
        # A backward branch past the SRS short range: RS AM=1 with the I bit
        # set, so the (positive) displacement is subtracted from the updated
        # IC.  BCT joins BC/BIX/BAL here -- its SRS short form (BCTB) only
        # reaches ~-56 halfwords, beyond which it needs this long form.
        # '$'-forced long (forceAM0) instead takes the AM=0 absolute-target form
        # below, even backward (e.g. 'BNZ$ FCMCSOUL' -> C3F3 <target>).
        data = generateRS1(stmt, operation, 0, 1, r1, 0x7FF & -d1, 0, ib2)
      elif not forceAM0 and \
              (x2 != None or ia or \
               i or (ib2 == 3 and selfRelB2 and d1 >= 0 and d1 < 2048) or \
               (ast.get("B2ONLY") and ib2 != 3 and not d2IsNumber and
                d1 >= 0 and d1 < 2048)):
        # RS AM=1 here.  The bare in-range-displacement case is AM=1 only when
        # PC/self-relative (ib2 == 3, no real base): an index (x2), indirect
        # (ia) or immediate (i) forces AM=1 regardless.  A reference over
        # a USING-RESOLVED base register with a displacement too large
        # for SRS belongs in AM=0 (16-bit displacement).  The COMMA form
        # 'expr(,Rn)' (B2ONLY, n != 3) with an expression displacement is
        # the OPPOSITE: AM=1 (the 11-bit AM=1 field bounds the rule;
        # larger falls through to AM=0).  The BARE form 'expr(Rn)' stays
        # out of this clause: it is base semantics -- SRS when it fits
        # (see optimizeScratch), legacy AM=0 beyond.  b2 == 3 is
        # excluded: a single paren register R3 rides the PC-relative d1 path
        # above, not a base.
        if ib2 == 3 and selfRelB2:
          # A genuinely self-relative target (ib2 == 3 is the PC sentinel,
          # not a real base) with a non-positive displacement takes the
          # subtractive I-bit form: 'BAL R3,*+2' (d1 = 0) encodes i=1,
          # d=0 (EA = IC - 0), never the additive form.  selfRelB2 is
          # required: 'STH R4,0(R5,3)' -- a real R3 BASE with an R5
          # index -- also reaches this clause (x2 != None) with ib2 == 3
          # and d1 == 0, and must stay additive.
          if d1 <= 0:
            d1 = -d1
            i = 1
        if x2 == None:
          x2 = 0
        if x2 < 0 or x2 > 7:
          error(stmt, "X2 out of range")
          x2 = 0
        data = generateRS1(stmt, operation, ia, i, r1, d1, x2, ib2)
      elif x2 == None and not ia and not i:
        # RS AM=0 here
        if b2 != None and originalD2 in rextrns:
          # Explicit base register, but the
          # displacement is an EXTRN symbol
          # (e.g. 'SCAL R0,#QEOUT(R3)') -- emit
          # zero displacement and let the
          # linker fill it via a Y-type RLD.
          d0 = 0
          if passCount == 3:
            emitDisplacementReloc(rextrns[originalD2])
        elif b2 != None:
          d0 = _num(d2) & 0xFFFF
          if selfRelB2 and passCount == 3:
            # The current-section fallback rewrote d2 to a csect-relative
            # offset (b2=3, no real base).  The AM=0 16-bit field holds
            # an ABSOLUTE in-bank address at run time, so it must be
            # relocated by the owning csect (csect base + offset, not the
            # bare offset).  Without this RLD every $-forced same-csect
            # branch in a linked image jumps to an unrelocated address.
            # (A real base-register displacement -- explicit or
            # USING-resolved -- is NOT relocated.)
            emitDisplacementReloc(sect)
        elif d2 in rextrns:
          b2 = 3
          d0 = 0
          if passCount == 3:
            emitDisplacementReloc(rextrns[d2])
        else:
          section, offset = unhash(d2)
          if section in extrns:
            # EXTRN symbol + constant offset
            # (e.g. 'LH R4,FAZ2STRT+3'): the
            # bare-EXTRN case above matches only
            # an exact hashcode, so here emit the
            # offset as displacement and let the
            # linker add the symbol via a Y RLD.
            b2 = 3
            d0 = offset & 0xFFFF
            if passCount == 3:
              emitDisplacementReloc(section)
          elif section in sects and \
                  sects[section].offset is not None:
            d0 = offset + sectionOffset(section) - sectionOffset(sect)
            b2 = 3
            if passCount == 3:
              # Find the actual CSECT for this offset
              # (expression evaluator may have rebased
              # to first CSECT's address space)
              rldSymbol = resolveCSect(d0, sects, section, exclude=sect)
              emitDisplacementReloc(rldSymbol)
          if b2 == None:
            error(stmt, "Could not interpret operand")
            return
        data = generateRS0(stmt, operation, r1, d0, b2)
      else:
        error(stmt, "Could not interpret line as SRS or RS")
    toMemory(data)
    return

  def handleCsectDsect():
    global firstCSECT
    nonlocal sect
    nonlocal cVsD
    # 'sect' is None until a START/CSECT/DSECT names a section ("" is the
    # unnamed section).  Code assembled while it is still None switches to
    # the unnamed section.
    cVsD = (operation == "CSECT")
    # The first *control* section wins, even when a DSECT precedes it (e.g.
    # 'GENERATE COPY=(...)' emits DSECT maps before the module's CSECT, so
    # 'sect' is already the DSECT, not None).  A DSECT is a dummy section and
    # never the firstCSECT; gate on 'firstCSECT is None', not 'sect == None'.
    if cVsD and firstCSECT is None:
      firstCSECT = name
      asmContext.firstCSECT = firstCSECT
    # A blank-name DSECT is the single "unnamed dummy section" (IBM allows it;
    # e.g. FCMBMASK's input-parameter-list map -- TFBMP/TBMPTYPE... read via
    # 'USING TFBMP,R0').  It must NOT reuse "" (that is the unnamed CSECT /
    # private code, a real control section), so map every bare DSECT to one
    # reserved key that cannot collide with a symbol-named section.
    protClose()
    sect = UNNAMED_DSECT if (name == "" and not cVsD) else name
    ensureSection(sect, dsect=not cVsD, esdSeq=stmt.n)
    if protOn and cVsD:
      protManaged.add(sect)
      protOpen[sect] = int(sects[sect].pos1)
    stmt.section = sect
    stmt.pos1 = sects[sect].pos1
    return

  def handleEntryExtrn():
    ast = stmt.ast
    if ast == None:
      error(stmt, f"Cannot parse operand of {operation}")
    else:
      # idlist yields a list of symbol strings; pass 0 may have replaced an
      # EXTRN's list with its filtered copy.
      for symbol in list(ast):
        if operation == "ENTRY":
          entries.setdefault(symbol)
          if symbol in symtab:
            symtab[symbol].entry = True
            if passCount == 3:
              if symtab[symbol].references is None:
                symtab[symbol].references = []
              symtab[symbol].references.append(stmt.n)
        else:
          registerExtrn(symbol, stmt.n)
    return

  def handleEqu():
    if name == "":
      error(stmt, "EQU has no name field")
      return
    toMemory(0)
    if name in symtab:
      if symtab[name].type != "EQU":
        error(stmt, f"EQU name already in use: {name}")
        return
    ast = stmt.ast
    if ast == None:
      error(stmt, "Cannot parse operand of EQU")
      return
    # EQU has a single value subfield; evaluate it directly rather than via the
    # dict-keyed evalInstructionSubfield (ast is an Equ dataclass, not an
    # instruction-operand dict).
    v = evalArithmeticExpression(ast.v, NO_LOCALS, stmt, symtab,
                                 currentHash(), severity=0)
    err = v is None
    if err:
      error(stmt, "Could not evaluate v subfield", severity=0)
    if operand.startswith("*") and sect is not None \
            and sect != firstCSECT and not sects[sect].dsect:
      # Drop the current section's term and re-base the '*' value
      # onto the first CSECT's address space (cf. expressions.py).
      # Only a *secondary* section needs rebasing; before the first
      # CSECT there is no section term to drop ('*' is just 0).  A DSECT
      # is NOT in the CSECT address space (its preliminaryOffset is never
      # set), so 'EQU *' there keeps its DSECT-relative value, un-rebased.
      v = (_num(v) & 0xFFFFFF) + symtab[firstCSECT].value \
          + symtab[sect].preliminaryOffset
    if err:
      error(stmt, "Cannot evaluate EQU")
      return
    # Do NOT trigger 'repeatPass' per EQU.  Convergence is judged once per
    # pass by the end-of-pass snapshot (EQU symbols are in it), so a settling
    # EQU still forces a repeat.  A per-line trigger would never converge: a
    # MULTIPLY-DEFINED EQU (e.g. DCHAR's MSG13x 'EQU *' emitted twice) writes
    # two '*' addresses every pass, so "did it change" is always true even
    # though the end-of-pass table (last-def-wins) is stable.
    symtab[name] = SymtabEntry(type="EQU", value=v, properties=stmt)
    applyResolvedValue(symtab[name], v)

  def handleLtorg():
    nonlocal literalPoolNumber
    if sect is None:
      # A literal pool belongs to a control section; diagnose up front
      # rather than let commonProcessing place a label in a section that
      # is in 'sects' but not 'symtab' (KeyError).
      error(stmt, "LTORG outside any control section")
      return
    commonProcessing(4)
    if collect and not asis:
      ltorg(sect)
    # A mid-csect pool occupies [pos1, pos1+size): record its origin (already
    # fullword-aligned by commonProcessing) and ADVANCE the location counter
    # past it -- every pass, so the layout is consistent across the compile
    # fixpoint.  IBM FIOLGERR: pool at hw 0x86, the two trailing DC Z ZCONs
    # at 0x88/0x8A, csect 140 hw; without the advance the ZCONs overlapped
    # the pool and the csect sized 136.  toMemory also gives the LTORG's
    # scratch/statement entry the pool's length, so optimizeScratch's
    # repositioning and the end-of-pass-1 length replay both account for it.
    pool = literalPools[literalPoolNumber]
    if pool.literals and pool.atLtorg:
      pool.offset = sects[sect].pos1
      toMemory(pool.size)
    literalPoolNumber += 1
    return

  def handleUsingDrop():
    if operation == "DROP" and stmt.operand.strip() == "":
      # A DROP with no operands releases every active base
      # register (and has no parsed operand AST).
      for r in range(len(using)):
        using[r] = None
      return
    ast = stmt.ast
    if ast == None or "r" not in ast or not isinstance(ast["r"], list):
      error(stmt, "Cannot parse operand of " + operation)
      return
    # Evaluate into a copy; ast["r"] keeps the unevaluated nodes (locAst
    # below is stored for later re-evaluation and must stay an AST).
    rlist = list(ast["r"])
    for i in range(len(rlist)):
      rlist[i] = evalArithmeticExpression(rlist[i], NO_LOCALS, \
                                          stmt, \
                                          symtab, currentHash())
    if operation == "USING":
      if len(rlist) < 1:
        error(stmt, "No value specified")
        return
      if rlist[0] == None:
        error(stmt, "Bad location")
        return
      else:
        h = rlist.pop(0)
        section, address = unhash(h)
        stmt.using = address
        # Keep the unevaluated location expression plus the eval context, so
        # optimizeScratch can re-resolve a forward-referenced base against
        # the settled symtab (see 'refreshUsing').
        locAst = ast["r"][0]
        starHash = currentHash()
    k = 0
    for r in rlist:
      if r == None or r < 0 or r > 7:
        error(stmt, "Bad register number")
      elif operation == "USING":
        using[r] = (h, section, address, locAst, k, stmt, starHash)
        h += 4096
        address += 4096
        k += 1
      else: # DROP
        using[r] = None
    return

  def handleCnop():
    # 'CNOP n' aligns the location counter to an n-halfword boundary (n=2 ->
    # fullword), e.g. before a fullword-aligned long-format branch;
    # commonProcessing pads like any alignment gap.  @CNOP/#CNOP are the same
    # pseudo-op in MSC/BCE (IOP) context -- the location counter is shared --
    # so they route here, not to the IOP encoder.
    align = 2
    if operand:
      nast = parse(operand, "arith")
      nval = None
      if nast is not None:
        nval = evalArithmeticExpression(nast, NO_LOCALS, stmt,
                                        symtab, currentHash())
      if nval:
        align = nval * 2
      if nval == 1:
        # CNOP 1 = ODD-halfword alignment ('FORCED ODD BOUNDRY
        # ALIGNMENT'): ONE filler hw when the counter is EVEN, so that
        # following skeletons assemble with zero displacements; nothing
        # emitted when the counter is already odd.  The pad is emitted as
        # real bytes (toMemory) so every sizing pass replays it via
        # stmt.length; a label would take the PRE-pad address.
        commonProcessing(1)
        if sect is not None and sect in sects \
                and (sects[sect].pos1.hw & 1) == 0:
          fill1 = 0xC0 if operation[0] in "@#" else 0xD8
          toMemory(bytearray([fill1, 0x00]))
        return
    # CNOP ("conditional no-op") fills the alignment gap with executable
    # NOPs, NOT a zero gap -- the pad sits in the instruction stream and may
    # be reached by fall-through.  CPU context (CNOP): BCF 0,0 = 0xD800.
    # IOP context (@CNOP/#CNOP): the DLYI-0 no-op = 0xC000 (a D800
    # there would be a CPU opcode in IOP instruction space).  Write the filler
    # halfwords over the gap (gated like toMemory: real bytes only in a
    # CSECT compile pass), then let commonProcessing advance the counter
    # and place any label at the aligned location.
    if cVsD and compile and sect is not None and sect in sects:
      fill_hi = 0xC0 if operation[0] in "@#" else 0xD8
      pos1 = sects[sect].pos1
      end = pos1.align(align)
      if end != pos1:
        memory = sects[sect].memory
        while end > len(memory):
          memory.extend(DEFAULT_CHUNK)
        for p in range(pos1, end, 2):
          memory[p] = fill_hi
          memory[p + 1] = 0x00
    commonProcessing(align)
    return

  def handleOrg():
    # ORG moves the location counter within the current section: 'ORG expr'
    # to expr's halfword address, 'ORG' with no operand to the section's
    # high-water mark.  No data is emitted -- pos1 just moves and a following
    # 'toMemory' overwrites at the new position.  The common backward
    # 'ORG *-1' lets a message-table macro reuse an FCW2 control halfword.
    if sect is None:
      # The location counter exists only inside a section; diagnose up
      # front (commonProcessing would otherwise KeyError on symtab[None]).
      error(stmt, "ORG outside any control section")
      return
    if name != "":
      commonProcessing()
    if operand.strip() == "":
      sects[sect].pos1 = sects[sect].used
    else:
      oast = parse(operand, "arith")
      if oast is None:
        error(stmt, "Cannot parse operand of ORG")
        return
      target = evalArithmeticExpression(oast, NO_LOCALS, stmt,
                                        symtab, currentHash())
      if target is None:
        error(stmt, "Cannot evaluate operand of ORG")
        return
      newPos1 = (target - symtab[sect].value) * 2
      if newPos1 < 0:
        error(stmt, "ORG before start of section")
        return
      sects[sect].pos1 = Addr(newPos1)
      if newPos1 > sects[sect].used:
        sects[sect].used = Addr(newPos1)
    return

  #-----------------------------------------------------------------------
  # Pass 0
  log(f"Pass 0: parsing operands ({len(source)} lines)")
  passCount = 0
  asmContext.passCount = passCount
  metadata["passCount"] = passCount
  sect = None
  using = [None]*8
        
  # Process source code, line-by-line
  for stmt, operation in processable(source, macros):
    if operation in ignore:
      continue
    operand = stmt.operand.rstrip()
    if operand != "":
      if operation in APPROPRIATE_RULES:
        ast = parse(operand, APPROPRIATE_RULES[operation])
        if ast == None:
          error(stmt, "Could not parse operands")
        stmt.ast = ast
        
    # Create "preliminary" symtab entries so the next pass can resolve
    # forward references (e.g. 'USING symbol,register').  Also: the original
    # assembler silently dropped EXTRN statements for already-defined
    # symbols -- not even listed -- so we detect and do the same.
    if operation == "EXTRN":
      # Only a single-symbol EXTRN gets pass-0 pre-registration and the
      # already-defined drop; a multi-symbol EXTRN ('EXTRN IOCODE,IOBUF')
      # is registered only by handleEntryExtrn.  Extending pass 0 to
      # multi-symbol lists changes output.
      ast = stmt.ast if operand != "" else None
      if isinstance(ast, list) and len(ast) == 1:
        # stmt.ast is shared with the parse cache; filter into a new list.
        kept = []
        for symbol in ast:
          if symbol in symtab:
            pass    # already defined: IBM silently drops (and delists) it
          else:
            makeExtern(symbol, stmt)
            kept.append(symbol)
        stmt.ast = kept
        if len(kept) == 0:
          stmt.empty = True
      continue
    if operation in ["EQU"]:
      continue
    name = stmt.name
    if name in [None, ""]:
      continue
    if operation in ["CSECT", "DSECT"]:
      sect = name
      if sect not in sects:
        ensureSection(sect, dsect=(operation == "DSECT"), esdSeq=stmt.n,
                      stmt=stmt, preliminary=True)
      elif passCount == 3:
        if symtab[sect].references is None:
          symtab[sect].references = []
        symtab[sect].references.append(stmt.n)
      continue
    if sect is None or sect not in sects:
      continue
    pos1 = sects[sect].pos1
    sects[sect].pos1 = pos1 + 4
    # LOAD-BEARING QUIRK: the preliminary value/address use the BYTE
    # offset where the real (pass-1) entries use halfwords, a 2x over-estimate.
    # Tested 2026-07-13: switching to '.hw' passes every small fixture but
    # breaks BILDNEW5 outright -- 6346 pass-1 "Eval error type 1" failures on
    # forward references (e.g. 'DC Y(GLITCH01)').  The pass-1 evaluator's
    # forward-reference lookahead depends on these byte-valued estimates.
    # It seems there's something wonky going on, but it's unclear what.
    symtab[name] = SymtabEntry(
        type=("DATA" if operation in ["DC", "DS"] else "INSTRUCTION"),
        section=sect, address=pos1.bytes,
        value=symtab[sect].value + pos1.bytes,
        alignment=4,
        preliminary=True, n=stmt.n,
        dsect=sects[sect].dsect, properties=stmt)

  #-----------------------------------------------------------------------
  # Remaining passes.  In principle these are passes 1 through 3.  However,
  # it is possible for values of symbols defined by EQU to change during
  # pass 3, in which case pass 3 is repeated (through pass 4, 5, ...) until
  # no more changes occur.
    
  repeatPass = False
  passCount = 0
  # Backstop against a non-convergent compile fixpoint (EQU re-evaluation or
  # SRS/RS branch condensing that fails to settle).  In principle the branch
  # condense is monotone (addresses only shrink) so this is never hit; if it
  # ever is, that is a bug worth surfacing rather than hanging.
  maxPasses = 50
  # Snapshot of the symbol table at the end of the previous compile pass, used
  # to decide whether the SRS/RS branch-condense fixpoint has settled.
  compileSnap = None
  while passCount < 3 or repeatPass:
    if passCount >= maxPasses:
      print(f"Warning: assembly did not converge after {maxPasses} passes "
            "(unexpected -- likely an assembler bug); the object code may "
            "be incorrect.", file=sys.stderr)
      break
    repeatPass = False
    passCount += 1
    metadata["passCount"] = passCount
    asmContext.passCount = passCount
    collect = (passCount in [1, 2])
    asis = (passCount == 2)
    compile = (passCount >= 3)

    if passCount == 1:
      log("Pass 1: placing sections, sizing instructions")
    elif passCount == 2:
      log("Pass 2: fixing up addresses")
    elif passCount == 3:
      log("Pass 3: emitting object code")
    else:
      log(f"Pass {passCount}: re-emitting (addresses still settling)")
        
    if asis:
      sects.clear()      # note: symtab is deliberately NOT cleared
    else:
      ppos1 = 0
      for sect in sects:
        if sect in symtab:
          if not sects[sect].dsect:
            ppos1 += ppos1 & 1
            symtab[sect].preliminaryOffset = ppos1
            ppos1 += sects[sect].used.hw
            # 'used' doesn't include the literal pool appended to the
            # section's end, so add it explicitly.  (A mid-csect LTORG
            # pool IS inside 'used' -- handleLtorg advances the location
            # counter past it -- so only the end-of-source pool counts.)
            for pool in literalPools:
              if pool.sect == sect and not pool.atLtorg:
                ppos1 += ppos1 & 1
                ppos1 += pool.size // 2
                break
        sects[sect].pos1 = Addr(0)
        # Reset 'used' along with pos1: each compile pass re-derives the
        # high-water mark from its own (possibly SHORTER) layout.  The
        # previous pass's value survives in 'usedPrev' for the mid-pass
        # size-estimate readers.  (The literal-pool flush after the final
        # pass still grows 'used' to cover the end-of-source pool.)
        sects[sect].usedPrev = sects[sect].used
        sects[sect].used = Addr(0)
    sect = None
    using = [None]*8
    literalPoolNumber = 0

    # SPON/SPOFF store-protect capture.  The directives toggle a persistent
    # per-halfword protect state (the real PSA interleaves them PSW cell by
    # PSW cell); the assembler's only output for them is the PROT control
    # cards emitted with the object (see assemble.writeObject).  protOn
    # persists across CSECT switches (FIOMFBCE-style decks re-assert it right
    # after CSECT precisely because it carries).  A csect is "managed" once a
    # toggle fires inside it or it opens with the state already on; managed
    # csects get a PROT card even with no protected ranges (the card's
    # presence tells the linker the source fully specifies protection).
    protOn = False
    protManaged = set()
    protRanges = {}                 # csect -> [(startByte, endByte), ...]
    protOpen = {}                   # csect -> currently-open range start

    def protClose():
      if sect is not None and sect in protOpen:
        start = protOpen.pop(sect)
        end = int(sects[sect].pos1)
        if end > start:
          protRanges.setdefault(sect, []).append((start, end))

    # Heartbeat through a large encoding pass: without it a multi-thousand-
    # line member looks frozen mid-pass under --verbose.  Gated to the slow
    # compile passes and big inputs so ordinary assemblies stay quiet.
    def heartbeat(n):
      if compile and n and len(source) > 5000 and n % 5000 == 0:
        log(f"  ... {n}/{len(source)} lines")

    # Process source code, line-by-line
    for stmt, operation in processable(source, macros, heartbeat=heartbeat):
      if operation in ("SPON", "SPOFF"):
        turnOn = (operation == "SPON")
        if sect is not None and sect in sects and not sects[sect].dsect:
          protManaged.add(sect)
          if turnOn != protOn:
            if turnOn:
              protOpen[sect] = int(sects[sect].pos1)
            else:
              protClose()
        protOn = turnOn
        continue
      if operation in ignore:
        continue

      name = stmt.name
      if name.startswith("."):
        name = ""
      operand = stmt.operand.rstrip()
            
      #******** Most pseudo-ops ********
      # Take care of pseudo-ops that don't add anything to AP-101 memory.
      # Each one of these should either 'continue' or 'break', so as not to
      # fall through to instruction processing.
      if operation in ["CSECT", "DSECT"]:
        handleCsectDsect()
        continue
      elif operation == "END":
        protClose()
        break
      elif operation in ["ENTRY", "EXTRN"]:
        handleEntryExtrn()
        continue
      elif operation == "EQU":
        handleEqu()
        continue
      # For EXTRN see ENTRY.
      elif operation == "LTORG":
        handleLtorg()
        continue
      elif operation in ["USING", "DROP"]:
        handleUsingDrop()
        continue
      elif operation in ("CNOP", "@CNOP", "#CNOP"):
        handleCnop()
        continue
      elif operation == "ORG":
        handleOrg()
        continue

      #******** Partial Alignment ********
      # All DC, DS, and instructions are at least halfword-aligned (the
      # listing prints halfword addresses).  The manual's talk of
      # "unaligned" C/X data refers only to suboperands after the first.
      if sect is None or sect not in sects:
        # No section defined yet - this instruction can't be processed.
        # Will be caught as "Unrecognized line" later or is an unknown
        # operation (possibly an undefined macro).
        error(stmt, f"Instruction outside of control section (undefined macro '{operation}'?)")
        continue
      sects[sect].pos1 = sects[sect].pos1.align(2)
            
      #******** Process instruction ********
            
      # For our purposes, pseudo-ops like 'DS' and 'DC' that can have labels,
      # modify memory, and move the instruction pointer are "instructions".
      if operation in ["DC", "DS"]:
        handleDcDs()
        continue

      if march != "ap101s" and base_op(operation) in AP101S_ONLY:
        error(stmt, f"'{operation}' is an AP-101S-only instruction, "
                    f"not available with --march {march}")
        continue
      if operation in ARGS_RR:
        handleRR()
        continue

      if operation in ARGS_SRS_OR_RS:
        handleSrsOrRs()
        continue
                
      if operation in ARGS_RI:
        handleRI()
        continue
      if operation in ARGS_SI:
        handleSI()
        continue
      if operation in ARGS_MSC or operation in ARGS_BCE:
        handleMSC_BCE()
        continue

      error(stmt, "Unrecognized line")
      continue
        
    if collect and not asis:
      # Close out the final literal pool
      endOfSource()
      # Rearrange "literals" in the literal pool according to alignment,
      # and figure out their offsets into the pools.  A mid-csect (LTORG)
      # pool was laid out at its LTORG (see 'ltorg') and its origin is the
      # LTORG's location -- only the default end-of-source pool is placed
      # here, after the csect's high-water mark.  IBM aligns the pool
      # ORIGIN to a FULLWORD, not merely to the pool's widest literal
      # (an all-halfword pool still starts on a fullword, with one gap
      # halfword when the code ends odd).
      for pool in literalPools:
        if not pool.literals or pool.atLtorg:
          continue
        layoutPool(pool)
        pool.offset = \
            sects[pool.sect].used.align(max(pool.alignment, 4))
      # Eliminate ambiguity between SRS and RS instructions.  The repetition
      # is required even though optimizeScratch iterates every section
      # itself: each rerun re-evaluates scratch EQUs at post-condensation
      # addresses, and pass 2 reads those values through forward references
      # before redefining them.  A single call changes output.
      for sect in sects:
        optimizeScratch()
      # optimizeScratch may have shrunk CSECTs (SRS < RS), which can move
      # LTORGs down.  It leaves no direct size info, so recompute each
      # section's size by replaying the per-line lengths over the source.
      for sect in sects:
        sects[sect].used = Addr(0)
        sects[sect].pos1 = Addr(0)
      for stmt in source:
        try:
          # ###FIXME### This doesn't account for the possibility of
          # 'ORG' pseudo-ops.
          sect = stmt.section
          alignment = stmt.alignment
          pos1 = sects[sect].pos1.align(alignment) + stmt.length
          sects[sect].pos1 = pos1
          if pos1 > sects[sect].used:
            sects[sect].used = pos1
        except:
          pass
      for pool in literalPools:
        if not pool.literals or pool.atLtorg:
          continue
        usage = sects[pool.sect].used
        offset = pool.offset
        # Walk the end-of-source pool back down to just above the replayed
        # high-water mark, in FULLWORD steps: the pool origin stays
        # fullword-aligned (IBM rule -- see the placement above).
        alignment = 4
        while offset - alignment >= usage:
          offset -= alignment
        pool.offset = offset
    if asis:
      # All CSECTs (not DSECTs) are laid out contiguously, each given an
      # 'offset' (an 'Addr' from the first CSECT's origin), fullword-realigned
      # between.  The exact rule for which sections are contiguous isn't
      # pinned down; treating all of them this way matches known behavior.
      lastOffset = 0                 # accumulated in halfwords
      for sect in sects:
        if sects[sect].dsect:
          continue
        sects[sect].offset = Addr.from_hw(lastOffset)
        offset = sects[sect].used
        for pool in literalPools:
          if pool.literals and pool.sect == sect:
            offset = pool.offset + pool.size
            break
        lastOffset += offset.hw
        lastOffset = (lastOffset + 1) & 0xFFFFFE

    # SRS/RS branch-condense fixpoint: repeat compile until the symbol table
    # stops changing.  Each pass re-resolves branches against the table as it
    # stood at the pass start; the first unchanged pass is the one where
    # every forward reference saw its target's final address.  Compared whole
    # once per pass (a multiply-defined label churns within a pass but is
    # stable at end-of-pass).  Code only shrinks, so it converges fast;
    # 'maxPasses' is a loud backstop if something grows instead.
    if compile:
      snap = { k: v.value for k, v in symtab.items() }
      if snap != compileSnap:
        repeatPass = True
      compileSnap = snap

  log(f"Object code converged after {passCount} passes; "
      f"{len(sects)} section(s), {len(symtab)} symbol(s)")

  # Let's append the literal pools to their CSECTs.
  for pool in literalPools:
    if not pool.literals:
      continue
    assembled = sects[pool.sect].memory
    desiredLength = pool.offset + pool.size
    actualLength = len(assembled)
    if actualLength < desiredLength:
      assembled.extend(bytearray(desiredLength - actualLength))
    # The alignment gap between the csect's code and an end-of-source
    # pool's fullword origin is covered by TXT and holds C9FB, like every
    # other CPU-csect alignment gap (see commonProcessing).
    if not pool.atLtorg:
      for i in range(int(sects[pool.sect].used), int(pool.offset)):
        assembled[i] = 0xC9 if (i & 1) == 0 else 0xFB
    for i, lit in enumerate(pool.literals):
      offset = pool.offset + pool.offsets[i]
      lassembled = lit.assembled
      assembled[offset:offset+len(lassembled)] = lassembled
      # A =Z literal's pool slot is a ZCON: emit a 'Z' RLD against its reloc
      # target so the linker fills HW0 (adding the in-image addend) and the
      # segment byte.  Other literal types carry no pool-site relocation.
      # A target defined in this assembly resolves here (the flush runs after
      # the final pass): its halfword address is added to the in-image addend
      # and the RLD targets its containing CSECT, mirroring the DC Z path --
      # only a still-undefined target becomes an ER.
      if lit.zsym is not None:
        zType = 'ZD' if lit.zdata else 'Z'   # data-subfield target -> 0x50
        litsym = symtab.get(lit.zsym)
        if litsym is None or litsym.type == "EXTERNAL":
          registerExtrn(lit.zsym)
          appendReloc(relocations, lit.zsym, pool.sect, offset, zType)
        else:
          relSect, relOff = unhash(litsym.value)
          zcon = ZCon.from_image(assembled, offset)
          if relSect is not None:
            combined = relOff + sectionOffset(relSect)
            zcon.hw0 = (zcon.hw0 + combined) & 0xFFFF
            zcon.write_to_image(assembled, offset)
            rldSymbol = resolveCSect(combined, sects, relSect,
                                     exclude=pool.sect)
            appendReloc(relocations, rldSymbol, pool.sect, offset, zType)
          elif relOff is not None:
            # Absolute (EQU) target: no RLD; sector-encode like the linker.
            zcon.hw0 = 0x8000 | ((zcon.hw0 + relOff) & 0x7FFF)
            zcon.write_to_image(assembled, offset)
          # A complex-relocatable target was already diagnosed at the
          # literal's use site; leave the slot's addend image as-is.
      elif lit.T == "Y" and isinstance(lit.value, Reloc):
        # A =Y literal over a RELOCATABLE value gets a pool-slot Y RLD,
        # mirroring DC Y / emitAddressConstant: a local target folds its
        # section offset into the halfword and relocates against the
        # containing CSECT; an EXTRN target keeps the addend image and the
        # linker adds the symbol.  (Previously the bare numeric image was
        # emitted with no RLD at all.)
        relSect, relOff = unhash(lit.value)
        if relSect is not None:
          ysym = symtab.get(relSect)
          if ysym is not None and ysym.type == "EXTERNAL":
            registerExtrn(relSect)
            appendReloc(relocations, relSect, pool.sect, offset, 'Y')
          else:
            combined = relOff + sectionOffset(relSect)
            assembled[offset:offset + 2] = \
                (combined & 0xFFFF).to_bytes(2, "big")
            rldSymbol = resolveCSect(combined, sects, relSect,
                                     exclude=pool.sect)
            appendReloc(relocations, rldSymbol, pool.sect, offset, 'Y')
        # A complex value (relSect None) keeps its numeric image, like the
        # =Z case above.
    # A mid-csect (LTORG) pool ends below the csect's high-water mark; only
    # GROW 'used' for the end-of-source pool -- never truncate back to a
    # mid pool's end (that clipped FIOLGERR's trailing ZCONs).
    if desiredLength > sects[pool.sect].used:
      sects[pool.sect].used = desiredLength

  protClose()                     # a deck with no END card still closes out
  metadata["protManaged"] = sorted(protManaged)
  metadata["protRanges"] = {s: sorted(r) for s, r in protRanges.items()}
  return metadata
