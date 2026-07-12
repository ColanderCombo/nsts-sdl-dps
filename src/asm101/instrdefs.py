#!/usr/bin/env python3
#
# Load the auto-extracted GPC instruction definitions (instr_defs.json) into
# descriptor-driven InstructionSets the assembler can encode from.
# 
import json
from pathlib import Path

from .instrset import Descriptor, InstructionSet

_JSON = Path(__file__).resolve().parent / "instr_defs.json"


def _load():
  return json.loads(_JSON.read_text())


_DEFS = _load()
CPU_DEFS = _DEFS["cpu"]
MSC_DEFS = _DEFS["msc"]
BCE_DEFS = _DEFS["bce"]

CPU_ALIASES = {"LACR": ("LCR", {})}  # LACR -> LCR, same instruction

CPU = InstructionSet({"CPU": {name: d["d"] for name, d in CPU_DEFS.items()}}, 
                     CPU_ALIASES)


def cpu_type(name):
  d = CPU_DEFS.get(name)
  if d is None:
    return None
  bits, _, flags = d["d"].partition("/")
  fields = set(c for c in bits if c not in "01_")
  if flags == "X":
    return "RS"
  if flags == "I":
    return "SI" if "d" in fields else "RI"
  return "SRS" if "d" in fields else "RR"


def cpu_mnemonics(*types):
  return frozenset(n for n in CPU_DEFS if cpu_type(n) in types)


def cpu_optype(name):
  d = CPU_DEFS.get(name)
  return None if d is None else d.get("opType")


def cpu_fp(name):
  d = CPU_DEFS.get(name)
  return None if d is None else d.get("fp")

# RS/SRS mnemonics whose RS long form uses NON-STANDARD (extended, indexed)
# displacement sentinels in place of (0x3c, 0x3d).  Not derivable: the sim
# hard-codes IAL's sentinels too (cpu.coffee decodef).
RS_SENTINELS = {"IAL": (0x3e, 0x3f)}

# The RS long form's SECOND halfword, as descriptors (the same strings as the
# sim's @rs_ae/@rs_ai): AM=0 a 16-bit displacement; AM=1 index reg (x),
# indirect (a), immediate (i), 11-bit displacement.
RS_HW2_EXTENDED = Descriptor("rs_ae", "dddddddddddddddd")
RS_HW2_INDEXED = Descriptor("rs_ai", "xxxaiddddddddddd")


def srs_d2_units(name):
  """SRS D2 unitizer: 2 when the 6-bit displacement counts fullwords, else 1
  (POO SRS scaling: EA = base + (D2 << (addrWidth-1))).  Reads the same
  'addrWidth' attribute (and absent->FULLWORD default) as the sim's g_EA."""
  d = CPU_DEFS.get(base_op(name))
  if d is None:
    return 1
  return 2 if d.get("addrWidth", "FULLWORD") == "FULLWORD" else 1


def base_op(name):
  return name.replace("@", "").replace("#", "")


def has_rs_descriptor(name):
  """True if `name` (or its @/#-stripped base) has a usable RS/SRS descriptor"""
  base = base_op(name)
  d = CPU.by_name.get(base)
  return (d is not None and d.nbits == 16 and not d.immediate
          and ({"d", "b"} <= set(d.fields)
               or {"a", "b"} <= set(d.fields)))


def implied_r1(name):
  """The R1 baked into a no-R1 SRS/RS op's fixed opcode bits -- descriptor bits 5-7
  (the R1 field position) -- for an op that takes NO explicit R1 operand (no 'x'
  field).  Returns None for an op with an explicit R1 ('x' field) or no SRS/RS
  descriptor; @/# variants resolve to the base.
  """
  base = base_op(name)
  d = CPU.by_name.get(base)
  if d is None or d.nbits != 16 or "x" in d.fields:
    return None
  if cpu_type(base) not in ("SRS", "RS"):
    return None
  return (d.val >> 8) & 0b111


def rs_hw1(name, r1, b2, indexed):
  """First halfword of an RS instruction from the descriptor, mirroring the GPC
  simulator's encode: an SRS-type op (with a 'd' displacement field) sets a
  SENTINEL displacement -- 0x3c extended, 0x3d indexed; an RS-type op ('/X',
  with an 'a' address-mode field) sets the explicit address-mode bit.  The
  caller builds the second halfword (16-bit displacement, or index|disp).
  """
  base = base_op(name)
  desc = CPU.by_name[base]
  fields = {"x": r1}
  if "d" in desc.fields:
    ext, idx = RS_SENTINELS.get(base, (0x3c, 0x3d))
    fields["d"] = idx if indexed else ext
    fields["b"] = b2
  elif indexed:
    fields["a"] = 1
    fields["b"] = b2
  else:
    fields["a"] = (b2 >> 2) & 1
    fields["b"] = b2 & 3
  return CPU.encode(base, fields)


def rs_form_bits(name):
  """The two SRS/RS form-selection bits that model101's codegen reads off the old
  ARGS_SRS_OR_RS 10-bit opcode -- derived from the descriptor instead.  Returns
  `(bit0, bit9)`:

    bit9  the most-significant opcode bit 
    bit0  the low bit of the RS form's `1111x` suffix (descriptor bits 8-12 of a
          `/X` form): 1 for an RS-ONLY op (`...11111...`, no SRS short form), 0
          for one that also has an SRS short (`...11110...`) or that is itself an
          SRS-only/short op.  An RS-only op is forced to AM=0.
   """
  base = base_op(name)
  desc = CPU.by_name.get(base)
  if desc is None:
    return 0, 1
  bit9 = (desc.val >> (desc.nbits - 1)) & 1
  if base in RS_SENTINELS:
    return 1, bit9             # IAL: RS-only '11111' suffix despite SRS desc
  # The `/X` RS form carries its 5-bit suffix at descriptor bits 7-3 (instruction
  # positions 8-12); its low bit distinguishes 11111 (RS-only) from 11110.  An
  # SRS-short-only descriptor (no 'a' field) has no such suffix -> bit0 = 0.
  bit0 = (desc.val >> 3) & 1 if "a" in desc.fields else 0
  return bit0, bit9

