#!/usr/bin/env python3
#
# IOP (MSC + BCE) instruction encoding for asm101.
# 
# The AP-101 GPC is two tightly-coupled processors: the CPU and the IOP 
# (Input/Output Processor) that runs in parallel and manages 24 serial 
# buses.  The IOP is itself a time-sliced multiprocessor with one MSC 
# ("Master Sequence Controller") and 24 BCEs ("Bus Control Elements").  
# MSC and BCE instructions are assembled inline with CPU code and live 
# in main memory, but each has its own limited instruction set and its own
# encoding, distinct from the CPU's.
#
# (Note: it looks like MSC and BCE programs actually live in their own
#  CSECTS separate from CPU code.  It's unclear how the original 
#  assembler handled this and whether you could interleave IOP code with
#  CPU code.  It's easy enough to support interleaving, though, so we do.)
# 
# This module supplies the IOP-specific conventions (aliases, PC-relative set,
# index/value-field helpers) and builds the MSC/BCE instruction sets.
# 
# The descriptor length (16 or 32) selects short vs long format -- format is fixed
# per mnemonic (unlike the CPU's operand-driven SRS/RX selection), so there is no
# length guessing.
# 
# Field-letter conventions:
#     a   18-bit absolute main-store address   (long format)
#     d   displacement / short field           (short format; PC-relative for
#                                               memory and branch instructions)
#     c   condition mask, count, or command
#     b   BCE number                           t  @CALL delta
#     u   interface-unit address (IUA)         l  @INT interrupt list
#     m   command, or a 1-bit addressing-mode bit
#     i   1 bit  -> index addressing-mode flag (set in source by a trailing "(1)")
#         N bits -> an immediate operand value
#     I   a 1-bit mode bit (e.g. @INT OR-with-X); not an operand, defaults to 0
#

from .instrset import InstructionSet, sign_extend
from . import instrdefs

MSC_DESC = {name: d["d"] for name, d in instrdefs.MSC_DEFS.items()}
BCE_DESC = {name: d["d"] for name, d in instrdefs.BCE_DEFS.items()}

# Branch-condition aliases.  @BC/@BXC take a literal condition operand; these
# mnemonics encode a fixed condition.  Condition bits (validated vs listing):
#     0x4 = test "= 0",  0x2 = test "< 0",  0x1 = test "> 0"   (OR of tests)
# so e.g. "not zero" = (<0 or >0) = 3, "not negative" = (=0 or >0) = 5.
ALIASES = {
    # accumulator branches -> @BC
    "@B":    ("@BC", 7),   # unconditional
    "@BZ":   ("@BC", 4),   # ACC = 0
    "@BNZ":  ("@BC", 3),   # ACC != 0
    "@BN":   ("@BC", 2),   # ACC < 0
    "@BNN":  ("@BC", 5),   # ACC >= 0
    "@BP":   ("@BC", 1),   # ACC > 0
    "@BNP":  ("@BC", 6),   # ACC <= 0
    # index branches -> @BXC
    "@BX":   ("@BXC", 7),  # unconditional
    "@BXZ":  ("@BXC", 4),  # X = 0
    "@BXNZ": ("@BXC", 3),  # X != 0
    "@BXN":  ("@BXC", 2),  # X < 0
    "@BXNN": ("@BXC", 5),  # X >= 0
    "@BXP":  ("@BXC", 1),  # X > 0
    "@BXNP": ("@BXC", 6),  # X <= 0
}

IOP = InstructionSet(
    {"MSC": MSC_DESC, "BCE": BCE_DESC},
    aliases={alias: (base, {"c": ccc}) for alias, (base, ccc) in ALIASES.items()},
)

MSC = IOP.procs["MSC"]
BCE = IOP.procs["BCE"]
ALL = IOP.by_name


# Base mnemonics whose short-format 'd' is a PC-RELATIVE displacement
# (EA = updated IC + d), from the descriptors' 'pcrel' attribute; the
# immediate/short siblings (#DLYI, #LTOI, #TDS/#RDS/#MIN/#MOUT) are literal.
PC_RELATIVE = frozenset(
    name
    for defs in (instrdefs.MSC_DEFS, instrdefs.BCE_DEFS)
    for name, d in defs.items() if d.get("pcrel"))


# The 1-bit descriptor fields that the trailing "(1)" index syntax sets.  The
# long-format MSC/BCE ops name it 'i' (index register); the BCE store-status ops
# #SSC/#SST name it 'm' (index mode).
_INDEX_FIELDS = ("i", "m")


def index_field(name):
  """The name of 'name's 1-bit index-mode field ('i' or 'm'), set by a trailing
  "(1)", or None if the op has none.  Lets the codegen handle the BCE 'm' ops the
  same way as the 'i' ops without hard-coding a single field letter."""
  desc = IOP.descriptor(name)
  if desc is None:
    return None
  for ch in _INDEX_FIELDS:
    if ch in desc.fields and desc.fields[ch][2] == 1:
      return ch
  return None


def value_fields(name):
  desc = IOP.descriptor(name)
  if desc is None:
    return []
  seen = []
  for ch in desc.raw:
    if ch in "01_" or ch in seen:
      continue
    seen.append(ch)
  return [ch for ch in seen if desc.fields[ch][2] > 1]
