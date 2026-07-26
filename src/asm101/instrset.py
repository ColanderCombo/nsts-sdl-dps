#!/usr/bin/env python3
#
# Generic AP-101 instruction-definition.
#
# A module for specifying instruction opcodes based on human-readable
# descriptor strings.  This is a python version of a similar module in
# nsts-sim-gpc.  instr_defs.json is extracted from the definitions in
# nsts-sim-gpc/gpc/*.instr.coffee and used by the assembler for encoding.# 
#
# An instruction is described by a *descriptor* string -- one character per bit,
# MSB first:# 
#
#     '0' / '1'   a fixed opcode bit
#     '_'         a don't-care bit (assembled as 0)
#     a letter    one bit of a named field; repeated letters span the field# 
#
# The descriptor length is the instruction width in bits (the CPU's RR/SRS 
# forms are 16 and RS/RI/SI long forms 32. 16 or 32 for the AP-101 IOP). 
#


class Descriptor:
  """One instruction's bit layout, parsed from its descriptor string into the
  fixed-bit mask/value and the per-field (shift, mask, width) placement.

  A descriptor may carry a trailing "/I", marking a CPU instruction that is 
  followed by a separate immediate-data halfword (the RI/SI forms)."""
  __slots__ = ("name", "raw", "bits", "nbits", "mask", "val", "fields",
               "flags", "immediate")

  def __init__(self, name, raw):
    bits, _, flags = raw.partition("/")
    if len(bits) not in (16, 32):
      raise ValueError(f"descriptor {raw!r} for {name} is {len(bits)} bits (need 16 or 32)")
    self.name = name
    self.raw = raw                  # the full descriptor incl. any "/flags"
    self.bits = bits                # just the bit string
    self.flags = flags              # the post-"/" string ("" if none)
    self.immediate = "I" in flags   # trailing immediate-data halfword (RI/SI)
    self.nbits = len(bits)
    mask = 0
    val = 0
    for ch in bits:
      mask = (mask << 1) | (1 if ch in "01" else 0)
      val = (val << 1) | (1 if ch == "1" else 0)
    self.mask = mask                # 1 where the bit is a fixed opcode bit
    self.val = val                  # the fixed opcode bits' values
    # field char -> (shift, field_mask, bits)
    fmask = {}
    fbits = {}
    n = len(bits)
    for j, ch in enumerate(bits):
      if ch in "01_":
        continue
      bitpos = n - 1 - j
      fmask[ch] = fmask.get(ch, 0) | (1 << bitpos)
      fbits[ch] = fbits.get(ch, 0) + 1
    fields = {}
    for ch, fm in fmask.items():
      shift = (fm & -fm).bit_length() - 1   # position of lowest set bit
      fields[ch] = (shift, fm, fbits[ch])
    self.fields = fields

  def pack(self, fields):
    """Pack field values (neg. two's complement OK) into the fixed opcode
    bits and return the big-endian bytes."""
    word = self.val
    for ch, (shift, fmask, _bits) in self.fields.items():
      v = fields.get(ch, 0)
      word = (word | ((v << shift) & fmask)) & ((1 << self.nbits) - 1)
    return word.to_bytes(self.nbits // 8, "big")


def sign_extend(value, bits):
  if value & (1 << (bits - 1)):
    return value - (1 << bits)
  return value


class InstructionSet:
  """Construct with:
    tables_by_proc:  {proc_name: {mnemonic: descriptor_string}}
    aliases:         {alias_mnemonic: (base_mnemonic, {field: forced_value})}
                     -- a mnemonic that encodes a base instruction with one or
                     more fields forced (e.g. a branch with a baked-in
                     condition). 
  """

  def __init__(self, tables_by_proc, aliases=None):
    self.procs = {proc: {name: Descriptor(name, raw)
                         for name, raw in table.items()}
                  for proc, table in tables_by_proc.items()}
    self.by_name = {}
    for table in self.procs.values():
      self.by_name.update(table)
    self.aliases = dict(aliases or {})
    # Per-(proc, width) match order, most-specific-mask first, so a fully
    # fixed opcode wins over a broader one.
    self._order = {
        proc: {nbits: self._match_order(table, nbits) for nbits in (16, 32)}
        for proc, table in self.procs.items()
    }

  @staticmethod
  def _match_order(table, nbits):
    items = [d for d in table.values() if d.nbits == nbits]
    items.sort(key=lambda d: d.mask, reverse=True)
    return items

  def is_op(self, name):
    return name in self.by_name or name in self.aliases

  def resolve(self, name):
    if name in self.aliases:
      base, forced = self.aliases[name]
      return base, dict(forced)
    return name, {}

  def descriptor(self, name):
    base, _ = self.resolve(name)
    return self.by_name.get(base)

  def encode(self, name, fields):
    """Encode instruction 'name' with the given field values into a
    big-endian 'bytes' object (2 or 4 bytes).  'fields' maps field letters
    to integer values (neg two's complement)"""
    base, forced = self.resolve(name)
    return self.by_name[base].pack(dict(fields) | forced)

  def decode(self, word, nbits, proc):
    """Identify the instruction whose fixed bits match 'word' (an 'nbits'-
    wide integer) within processor 'proc' and return (name, {field: value}).
    Returns (None, {}) if no descriptor matches."""
    for desc in self._order[proc][nbits]:
      if (word & desc.mask) == desc.val:
        fields = {}
        for ch, (shift, fmask, _bits) in desc.fields.items():
          fields[ch] = (word & fmask) >> shift
        return desc.name, fields
    return None, {}
