#!/usr/bin/env python3
#
import re
from dataclasses import dataclass, field, InitVar
from functools import cache

from .cardreader import lineContinues

# Card field split: the label (cols from 1), then the operation, each a run of
# non-blanks separated by blanks.  `[^ ]`/` ` (not `\S`/`\s`) keeps the
# space-only semantics -- a tab stays part of a field, it does not separate.
_FIELDS_RE = re.compile(r"([^ ]*) *([^ ]*) *")


@cache
def _split_card(line, nextline):
  """Split an 80-column card (`line`, already line-ending-free) into
  (text=cols 1-71, identification=cols 73-80, continues, empty, fullComment,
  dotComment).  `continues` looks one card ahead at `nextline`."""
  padded = f"{line[:80]:<80}"
  text = padded[:71]
  return (text, padded[72:], lineContinues(line, nextline),
          text.strip() == "",       # empty
          text.startswith("*"),     # fullComment
          text.startswith(".*"))    # dotComment


@cache
def _scan_fields(text):
  """Split card `text` into (name, operation, operand_start_column)."""
  m = _FIELDS_RE.match(text)        # always matches (groups can be empty)
  return (m.group(1), m.group(2), m.end())


@dataclass(slots=True)
class Statement:
  line: InitVar[str] = ""
  nextline: InitVar[str] = ""

  file: object = None
  macro: object = None
  lineNumber: int = 0
  inMacroDefinition: bool = False
  copy: object = None
  printable: bool = False
  depth: int = 0
  n: int = 0

  text: str = field(init=False, default="")            # card columns 1-71
  identification: str = field(init=False, default="")  # card columns 73-80
  continues: bool = field(init=False, default=False)   # continues onto nextline

  _empty: bool = field(init=False, default=False)
  _fullComment: bool = field(init=False, default=False)
  _dotComment: bool = field(init=False, default=False)

  section: object = None
  pos1: object = None      # location counter at this line: an 'Addr', or None
  length: object = None
  alignment: int = 2
  name: str = ""
  operation: str = ""
  operand: str = ""
  # The parsed operand AST, attached by the parse pass (larkparse.parse) and consumed
  # by codegen.  Heterogeneous (dict / list / DcSuboperand / str), or None when
  # the operand could not be parsed or the line has none.  None == not parsed.
  ast: object = None
  endComment: str = ""
  # USING base address, set on a USING directive and read by the listing's
  # address column for that line.  Only USING lines read it; 0 is the unused
  # default (matches the old stmt.get("using", 0)).
  using: int = 0
  errors: list = field(default_factory=list)
  # Codegen output: the assembled object bytes and the resolved operand
  # addresses shown in the listing.  None means "not produced for this line"
  assembled: object = None
  adr1: object = None
  adr2: object = None
  # Set by normalizeLongFormat when a trailing '$' forces the RS (long) form;
  # read during encoding to suppress the shorter SRS form.  Absent == False.
  longFormat: bool = False
  # Marks a line the parser pulled in but codegen/the listing must pass over
  # (a library-scan-skipped macro op, or a card consumed by a continuation).
  # Only ever set True; the codegen passes gate on it.  Absent == False.
  skip: bool = False
  # Set True when an MNOTE pseudo-op rewrites this card into a comment; the
  # listing reads it to format the generated line.  Only ever set True;
  # absent == False.
  mnote: bool = False
  # The scratch (DS/DC storage) descriptor dict attached during layout; read
  # by the cross-reference listing for the symbol's length.  None == not set.
  scratch: object = None
  # The 1-based line number this card was given in the printed listing, set as
  # the listing is emitted and read back by the cross-reference page (symbol
  # definition + reference line numbers).  None == the card was never printed.
  printedLineNumber: object = None

  def __post_init__(self, line, nextline):
    # `continues` and the text-derived flags are frozen here (computed once via
    # the memoized _split_card, not recomputed on read; see the module docstring).
    (self.text, self.identification, self.continues,
     self._empty, self._fullComment, self._dotComment) = _split_card(line, nextline)

  # --- text-derived flags ---
  @property
  def empty(self):
    return self._empty

  @empty.setter
  def empty(self, value):
    self._empty = value

  @property
  def fullComment(self):
    return self._fullComment

  @fullComment.setter
  def fullComment(self, value):
    self._fullComment = value

  @property
  def dotComment(self):
    return self._dotComment

  @property
  def is_blank_or_comment(self):
    return self._empty or self._fullComment or self._dotComment

  # --- listing render (per-line) ---
  def objcode_hex(self):
    data = self.assembled
    if not data:
      return ""
    dc = (self.operation == "DC")
    out = ""
    for j in range(min(8, len(data))):
      if j == 0 or ((j & 1) == 0 and not dc):
        out += " "
      out += f"{data[j]:02X}"
    return out

  def listing_address(self, symtab, offset, num):
    op = self.operation
    if op == "EQU": # -> symbol value
      name = self.name
      return (f"{num(symtab[name].value) & 0xFFFFFFF:07X}"
              if name in symtab else "")
    if op == "USING": # -> base
      return f"{self.using:07X}"
    if op == "LTORG": # -> null
      return ""
    if self.pos1 is not None:
      return f"{self.pos1.hw + offset:05X}"
    return ""

  def listing_prefix(self, symtab, offset, num):
    """<ADDR FIELD> <OBJCODE> <OPERAND ADDR'S> """
    prefix = self.listing_address(symtab, offset, num) + self.objcode_hex()
    if self.adr1 is not None:
      prefix = f"{prefix:<21}{self.adr1:04X}"
    if self.adr2 is not None:
      prefix = f"{prefix:<26}{self.adr2:04X}"
    return prefix

  # --- field parsing ---
  def parse_fields(self):
    """returns column of operand field (first non-blank after op)"""
    self.name, self.operation, end = _scan_fields(self.text)
    return end
  