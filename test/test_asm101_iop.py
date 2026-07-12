#!/usr/bin/env python3
"""Validate the IOP (MSC/BCE) instruction encoder against real flight-software
object code.

Ground truth is the RELIST/AP101S assembly listings shipped with the BFS flight
software:
    <repo>/../pfs/PFS/BFS.SRC as received/COMPILED/MSC
    <repo>/../pfs/PFS/BFS.SRC as received/COMPILED/BCE
Each listing line carries the assembler's LOC, the emitted OBJECT CODE, two
resolved-address columns (ADR1 = effective/target address, ADR2 = a printed
displacement that is unreliable for signed values), the statement sequence
number, and the source statement.

For every MSC/BCE machine-instruction line we run four checks against
asm101.iop_instr:

  A. opcode identity  -- decoding the object word by fixed-bit mask yields the
     same mnemonic as the source (branch aliases resolve to @BC/@BXC).
  B. field round-trip -- re-encoding the decoded fields reproduces the bytes.
  C. literal operands -- decoded value fields equal the literal operands written
     in the source (immediates, counts, deltas, BCE numbers, conditions, IUAs),
     and the 1-bit index flag matches a trailing "(1)".
  D. addresses        -- long-format absolute address == ADR1; short PC-relative
     displacement, sign-extended, == ADR1 - (loc+1).

Directives (@CNOP/#CNOP/...) and macros are skipped: only mnemonics the encoder
recognizes are checked.

Run:  PYTHONPATH=src build/venv/bin/python test/test_asm101_iop.py
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "asm101"))

from asm101 import iop_instr as iop

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LISTINGS = [
    os.path.join(REPO, "..", "pfs", "PFS", "BFS.SRC as received", "COMPILED", "MSC"),
    os.path.join(REPO, "..", "pfs", "PFS", "BFS.SRC as received", "COMPILED", "BCE"),
]

# Short instructions whose displacement field 'd' is PC-relative
# (EA = updated-PC + displacement).  Used by check D.  Taken straight from the
# encoder so the two never drift; the BCE memory-reference ops (#WIX etc.) make
# check D validate their listing displacement instead of skipping it as symbolic.
PC_RELATIVE = iop.PC_RELATIVE

_INT_RE = re.compile(r"^-?\d+$")
_HEX_RE = re.compile(r"^X'([0-9A-Fa-f]+)'$")


def parse_literal(tok):
  """Return the integer value of an operand token, or None if it is symbolic
  (a label or expression that needs the symbol table to resolve)."""
  m = _HEX_RE.match(tok)
  if m:
    return int(m.group(1), 16)
  if _INT_RE.match(tok):
    return int(tok)
  return None


def value_fields(desc):
  """Field letters that carry positional operands, in source (left-to-right)
  descriptor order: every multi-bit field.  1-bit fields are addressing-mode
  bits (index / OR-with-X), set by syntax rather than a positional operand."""
  seen = []
  for ch in desc.raw:
    if ch in "01_":
      continue
    if ch in seen:
      continue
    seen.append(ch)
  return [ch for ch in seen if desc.fields[ch][2] > 1]


def split_operand(operand):
  """Split an operand field into comma-separated tokens and an optional
  trailing index value from a "(N)" suffix on the last token."""
  index = None
  m = re.match(r"^(.*)\((\d+)\)$", operand)
  if m:
    operand = m.group(1)
    index = int(m.group(2))
  toks = operand.split(",") if operand else []
  return toks, index


class Result:
  def __init__(self):
    self.checked = 0
    self.passed = 0
    self.fail = []      # (file, lineno, mnemonic, message)
    self.ops = {}       # mnemonic -> count
    self.skipped = {}   # mnemonic -> count (recognized but unparseable line)

  def note_op(self, name):
    self.ops[name] = self.ops.get(name, 0) + 1


def parse_line(line):
  """Extract (loc, obj_bytes, adr1, adr2, mnemonic, operand) from a listing
  line, or None if it is not an MSC/BCE machine-instruction line.

  Fixed listing columns (1-based): col 1 = ASA carriage control; cols 2-6 =
  LOC (5 hex); cols 8-21 = OBJECT CODE; cols 22-26 = ADR1; cols 28-31 = ADR2;
  cols 33-36 = statement sequence number; cols 38-45 = name field; col 47 =
  operation field; operand follows.  Reading the operation by column avoids
  mistaking a mnemonic that appears in a comment (e.g. on a DC) for the op."""
  if len(line) < 47:
    return None
  loc_s = line[1:6].strip()
  obj_s = line[6:21]
  adr1_s = line[21:27].strip()
  adr2_s = line[27:32].strip()
  if not loc_s:
    return None
  try:
    loc = int(loc_s, 16)
  except ValueError:
    return None
  op_toks = line[46:].split()       # operation field is column 47
  if not op_toks:
    return None
  mnem = op_toks[0]
  if not iop.IOP.is_op(mnem):
    return None
  operand = op_toks[1] if len(op_toks) > 1 else ""
  # Object code: groups of hex in the OBJECT CODE column.
  groups = obj_s.split()
  if not groups or any(not re.fullmatch(r"[0-9A-Fa-f]+", g) for g in groups):
    return None
  objhex = "".join(groups)
  if len(objhex) not in (4, 8):     # 16- or 32-bit instruction word
    return None
  return {
      "loc": loc,
      "obj": bytes.fromhex(objhex),
      "adr1": int(adr1_s, 16) if adr1_s else None,
      "adr2": int(adr2_s, 16) if adr2_s else None,
      "mnem": mnem,
      "operand": operand,
  }


def check_line(rec, fname, lineno, res):
  mnem = rec["mnem"]
  res.note_op(mnem)
  res.checked += 1
  word = int.from_bytes(rec["obj"], "big")
  nbits = len(rec["obj"]) * 8
  base, forced = iop.IOP.resolve(mnem)
  desc = iop.ALL[base]

  def fail(msg):
    res.fail.append((fname, lineno, mnem, msg))

  # Decode the fields using the KNOWN (source) mnemonic's own descriptor, so
  # field letters are always this instruction's.
  dec_fields = {ch: (word & fm) >> sh
                for ch, (sh, fm, _b) in desc.fields.items()}

  # A. opcode identity (within this mnemonic's own processor instruction set).
  # A bit-matched descriptor that is not `base` is accepted only when it is
  # bit-equivalent -- same fixed-bit mask and value -- i.e. a format-identical
  # companion such as the #MINC/#MOUTC message command words.
  proc = "MSC" if base.startswith("@") else "BCE"
  dec_name, _ = iop.IOP.decode(word, nbits, proc)
  if dec_name != base:
    other = iop.ALL.get(dec_name)
    if other is None or other.mask != desc.mask or other.val != desc.val:
      fail("opcode id: object %s decoded as %s, expected %s"
           % (rec["obj"].hex().upper(), dec_name, base))
      return

  # B. field round-trip
  rt = iop.IOP.encode(base, dec_fields)
  if rt != rec["obj"]:
    fail("round-trip: re-encode -> %s != %s"
         % (rt.hex().upper(), rec["obj"].hex().upper()))
    return

  # C. literal operands + index flag
  toks, index = split_operand(rec["operand"])
  vfields = value_fields(desc)
  for ch, tok in zip(vfields, toks):
    # Alias-forced fields (the branch condition) are validated separately.
    if ch in forced:
      continue
    lit = parse_literal(tok)
    if lit is None:
      continue   # symbolic -> validated by check D
    shift, fmask, bits = desc.fields[ch]
    expected = lit & ((1 << bits) - 1)
    if dec_fields.get(ch, 0) != expected:
      fail("field %s: decoded %d, source operand %r -> %d"
           % (ch, dec_fields.get(ch, 0), tok, expected))
  # alias condition
  for ch, want in forced.items():
    if dec_fields.get(ch, 0) != want:
      fail("alias %s: condition field %s decoded %d, expected %d"
           % (mnem, ch, dec_fields.get(ch, 0), want))
  # index flag: a 1-bit 'i' field
  if "i" in desc.fields and desc.fields["i"][2] == 1:
    want_i = 1 if index else 0
    if dec_fields.get("i", 0) != want_i:
      fail("index flag: decoded %d, source %s"
           % (dec_fields.get("i", 0), "(1)" if index else "none"))

  # D. addresses
  if "a" in desc.fields and rec["adr1"] is not None:
    bits = desc.fields["a"][2]
    if dec_fields.get("a", 0) != (rec["adr1"] & ((1 << bits) - 1)):
      fail("abs addr: decoded %05X, ADR1 %05X"
           % (dec_fields.get("a", 0), rec["adr1"]))
  if base in PC_RELATIVE and rec["adr1"] is not None and "d" in desc.fields:
    bits = desc.fields["d"][2]
    disp = iop.sign_extend(dec_fields.get("d", 0), bits)
    want = rec["adr1"] - (rec["loc"] + 1)
    if disp != want:
      fail("pc-rel disp: decoded %d, ADR1 %05X - (loc+1 %05X) = %d"
           % (disp, rec["adr1"], rec["loc"] + 1, want))

  if not [f for f in res.fail if f[1] == lineno and f[0] == fname]:
    res.passed += 1


def selftest():
  """Descriptor integrity + encode/decode round-trip, independent of the
  listings.  Catches field bits that overlap fixed bits or each other, and
  pack/unpack asymmetry -- including for tentative descriptors (@RAI, @STP,
  @SEC) that the flight listings do not exercise."""
  errors = []
  for proc, table in (("MSC", iop.MSC), ("BCE", iop.BCE)):
    for name, desc in table.items():
      union = 0
      for ch, (sh, fm, _b) in desc.fields.items():
        if fm & desc.mask:
          errors.append("%s: field %s overlaps fixed bits" % (name, ch))
        if fm & union:
          errors.append("%s: field %s overlaps another field" % (name, ch))
        union |= fm
      # round-trip a walking value per field
      fields = {ch: (0b10110 & ((1 << b) - 1))
                for ch, (sh, fm, b) in desc.fields.items()}
      obj = iop.IOP.encode(name, fields)
      word = int.from_bytes(obj, "big")
      if (word & desc.mask) != desc.val:
        errors.append("%s: fixed bits not preserved after encode" % name)
      back_name, back_fields = iop.IOP.decode(word, desc.nbits, proc)
      bf = {ch: (word & fm) >> sh for ch, (sh, fm, _b) in desc.fields.items()}
      if bf != fields:
        errors.append("%s: round-trip fields %s != %s" % (name, bf, fields))
  # alias conditions round-trip
  for alias, (base, ccc) in iop.ALIASES.items():
    obj = iop.IOP.encode(alias, {"d": 0x2A})
    word = int.from_bytes(obj, "big")
    desc = iop.ALL[base]
    sh, fm, _b = desc.fields["c"]
    if (word & fm) >> sh != ccc:
      errors.append("alias %s: condition %d not encoded" % (alias, ccc))
  return errors


def parser_parity():
  """Parse every real MSC/BCE operand with the Lark front-end (mscAll for
  @-ops, bceAll for #-ops) and assert acceptance
  acceptance.  The Lark-migration gate (BILDNEW5 + the MLIB80 decks) has no IOP
  instructions, so the mscAll/bceAll grammar is otherwise unexercised; the
  flight listings are the real operand corpus.  Every real operand must parse
  (the fastparse-vs-Lark differential that originally validated these rules is
  gone with fastparse, so this is now a Lark regression guard), and the crafted
  reject cases must be declined."""
  from asm101 import larkparse
  from asm101.model101tables import ARGS_MSC, ARGS_BCE

  def lark(text, rule):
    return larkparse.parse(text, rule) is not None

  cases = []   # (rule, operand)
  seen = set()
  for path in LISTINGS:
    if not os.path.exists(path):
      continue
    with open(path, "rt", errors="replace") as f:
      for line in f:
        toks = line[46:].split()
        if len(toks) < 2:
          continue
        op, operand = toks[0], toks[1]
        rule = "msc" if op in ARGS_MSC else \
               "bce" if op in ARGS_BCE else None
        if rule and (rule, operand) not in seen:
          seen.add((rule, operand))
          cases.append((rule, operand))
  # crafted cases: valid-with-comment (Stage-0), and rejects.
  synthetic = [
      ("msc", "BCEMSKO(1)      WAIT FOR INDICATOR", True),
      ("msc", "8,BINIT2        LOAD PC", True),
      ("msc", "SYM,SYM", False),     # 1st operand of the pair must be an iopconst
      ("msc", "8,9", True),          # 2nd operand may be a numeric address
                                     # (PCGEN emits `@LBP 11,0` -- the "no PC"
                                     # sentinel, a literal 0 address)
      ("bce", "13,X'46060'    CMD", True),
      ("bce", "A,B,C", False),       # bceAll takes at most two operands
  ]

  mismatches = []
  for rule, operand in cases:
    if not lark(operand, rule):       # every real operand must parse
      mismatches.append((rule, operand, "accept", "REJECTED"))
  for rule, operand, want in synthetic:
    if lark(operand, rule) != want:
      mismatches.append((rule, operand, "want=%s" % want, "got=%s" % (not want)))
  return len(cases), mismatches


def run():
  res = Result()
  self_errs = selftest()
  print("IOP descriptor self-test (integrity + round-trip)")
  if self_errs:
    for e in self_errs:
      print("   FAIL", e)
    print("  %d error(s)" % len(self_errs))
  else:
    print("  ALL PASS (%d MSC + %d BCE descriptors, %d aliases)"
          % (len(iop.MSC), len(iop.BCE), len(iop.ALIASES)))
  print()

  print("MSC/BCE operand parser acceptance (Lark)")
  n_cases, parity_mismatch = parser_parity()
  print("  operand cases: %d" % n_cases)
  if parity_mismatch:
    for m in parity_mismatch[:30]:
      print("   MISMATCH", m)
    print("  %d mismatch(es)" % len(parity_mismatch))
  else:
    print("  ALL PASS")
  print()
  missing = [p for p in LISTINGS if not os.path.exists(p)]
  if missing:
    print("SKIP: listing(s) not found (descriptor self-test still ran):")
    for p in missing:
      print("   ", p)
    return 1 if (self_errs or parity_mismatch) else 0
  for path in LISTINGS:
    with open(path, "rt", errors="replace") as f:
      for lineno, line in enumerate(f, 1):
        rec = parse_line(line.rstrip("\n"))
        if rec is None:
          continue
        check_line(rec, os.path.basename(path), lineno, res)

  print("IOP encoder validation against BFS MSC/BCE listings")
  print("  instruction lines checked: %d" % res.checked)
  print("  passed:                    %d" % res.passed)
  print("  distinct mnemonics:        %d" % len(res.ops))
  if res.fail:
    print("  FAILURES: %d" % len(res.fail))
    # group by mnemonic+message head for readability
    shown = {}
    for fname, lineno, mnem, msg in res.fail:
      key = (mnem, msg.split(":")[0])
      shown.setdefault(key, []).append((fname, lineno, msg))
    for (mnem, head), items in sorted(shown.items()):
      fname, lineno, msg = items[0]
      print("   [%s] %s:%d  %s   (%d occurrence(s))"
            % (mnem, fname, lineno, msg, len(items)))
    return 1
  if self_errs or parity_mismatch:
    return 1
  print("  ALL PASS")
  # coverage summary
  print("  per-mnemonic counts:")
  for name in sorted(res.ops):
    print("     %-7s %4d" % (name, res.ops[name]))
  return 0


if __name__ == "__main__":
  sys.exit(run())
