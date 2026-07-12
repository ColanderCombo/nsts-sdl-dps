#!/usr/bin/env python3
#
# These are the nodes the Lark front-end ('larkparse.py') emits for every
# arithmetic / boolean / character expression, and that 'expressions.py'
# evaluates. 
#
# This file is partially based on ASM101S/expressions.py from 
# virtualagc by Ronald Burkey:
#   https://github.com/virtualagc/virtualagc/blob/master/ASM101S/expressions.py
# 
# It adapts the expression evaluators and places them within the AST node
# objects.
#
#
#   Arithmetic / values
#     Int(n)                     literal or self-defining term, value computed
#     Sym(name)                  assembler symbol  (resolved via symtab / '*')
#     Loc()                      location counter  '*'
#     Var(name, (idx...))        macro/SET variable &A, &A(i), &A(i,j)
#     Attr(c, operand)           attribute  c in N K L S I  of operand
#     BinOp(op, left, right)     + - * /  (left associative)
#     Neg(node)                  unary minus
#     Lcon(type, body, ...)      RS literal  =F'1' / =X'AB' / =Y(label) / ...
#   Boolean
#     Bool(True|False)           literal 0 / 1
#     Not(node) And((..)) Or((..))
#     Rel(op, left, right)       EQ NE LT GT LE GE
#     Defined(name)              D'sym
#   Character
#     Str(text)                  quoted string 
#     Concat((parts))            concatenation
#     Substr(source, start, len) 's'(start,len)
#     Tattr(operand)             T' type attribute  (operand is a Sym or Var)
# 

from dataclasses import dataclass
from ap101Utils import codepages  # noqa: F401  registers the "ebcdicvagc" codec


# Sentinel "section" key for a relocation term that has no real control section:
# marks a result not expressible as a relocation (e.g. relocatable*relocatable, or
# a relocatable operand of /).  Any Reloc carrying it unhashes as "complex".  Real
# ESD-ids (from model101's 'getHashcode') are positive ints from 1, so this object
# never collides.
POISON = object()


class Reloc:
  '''
  A relocatable assembly-expression value: a plain numeric part plus a multiset
  of relocation terms '{esd_id: signed_coefficient}'.  The Python analogue of the
  IBM assembler's IEUF8V 'RLIST' -- the (sign, ESD-id) term entries it
  accumulates while walking an expression.

  A symbol enters arithmetic as 'Reloc(offset, {esd_id: +1})'; ordinary '+', '-',
  '*', unary '-' then perform the relocatability algebra for free:
    * 'A - B' in one section cancels the term -> a bare int (absolute);
    * 'A + B' / 'A*2' leave a coefficient that is not a single +1 -> "complex";
    * 'A + const' keeps the single +1 term -> "simply relocatable".
  The macro/SETA path never sees a Reloc; one appears only once a relocatable
  *symbol* is referenced.  Results collapse back to a bare int the instant all
  terms cancel (see '_wrap'), so absolute values are always plain ints -- callers
  compare/mask them with no special-casing, and 'isinstance(x, Reloc)' is exactly
  the "is this relocatable?" test.
  '''
  __slots__ = ("num", "terms")

  def __init__(self, num=0, terms=None):
    self.num = num
    self.terms = {k: v for k, v in (terms or {}).items() if v}

  @staticmethod
  def _wrap(num, terms):
    # Collapse to a plain int when no relocation term survives (absolute).
    live = {k: v for k, v in terms.items() if v}
    return Reloc(num, live) if live else num

  def __add__(self, o):
    if isinstance(o, Reloc):
      t = dict(self.terms)
      for k, v in o.terms.items():
        t[k] = t.get(k, 0) + v
      return Reloc._wrap(self.num + o.num, t)
    return Reloc._wrap(self.num + o, dict(self.terms))
  __radd__ = __add__

  def __sub__(self, o):
    if isinstance(o, Reloc):
      t = dict(self.terms)
      for k, v in o.terms.items():
        t[k] = t.get(k, 0) - v
      return Reloc._wrap(self.num - o.num, t)
    return Reloc._wrap(self.num - o, dict(self.terms))

  def __rsub__(self, o):  # o - self, with o a bare int
    return Reloc._wrap(o - self.num, {k: -v for k, v in self.terms.items()})

  def __neg__(self):
    return Reloc(-self.num, {k: -v for k, v in self.terms.items()})

  def __mul__(self, o):
    if isinstance(o, Reloc):
      # relocatable * relocatable is never a single relocation.
      return Reloc(self.num * o.num, {POISON: 1})
    return Reloc._wrap(self.num * o, {k: v * o for k, v in self.terms.items()})
  __rmul__ = __mul__

  def __eq__(self, o):
    # A live Reloc is never equal to a bare int (or None); equality is used to
    # test 'value != 0 / != -1' and for '... in rextrns' membership.
    if isinstance(o, Reloc):
      return self.num == o.num and self.terms == o.terms
    return False

  def __ne__(self, o):
    return not self.__eq__(o)

  def __hash__(self):
    return hash((self.num, frozenset(self.terms.items())))

  def __repr__(self):
    return f"Reloc({self.num}, {self.terms!r})"


# ---- integer-value helpers ----
def isSignedInt(s):
  return isinstance(s, str) and \
      (s.isdigit() or (s.startswith("-") and s[1:].isdigit()))


# True if 's' is an (unsigned) self-defining term -- a decimal, hexadecimal
# (X'..'), binary (B'..'), or character (C'..') literal.  These are the operand
# values the type attribute T' reports as 'N'.
def isSelfDefiningTerm(s):
  if not isinstance(s, str):
    return False
  if s.isdigit():
    return True
  return len(s) >= 3 and s[0] in "XBC" and s[1] == "'" and s[-1] == "'"

# Evaluate a self-defining-term string -- X'..' hex, B'..' binary, C'..' EBCDIC
# character, or a bare decimal -- to its integer value.
def selfDefiningValue(s):
  if s.isdigit():
    return int(s)
  kind, body = s[0], s[2:-1]
  if kind == "X":
    return int(body, 16)
  if kind == "B":
    return int(body, 2)
  # C'..' : pack big-endian as EBCDIC, un-doubling the '' apostrophe escape.
  return int.from_bytes(body.replace("''", "'").encode("ebcdicvagc"), "big")


# Coerce 'value' to an int: pass a real int through, parse a signed-int string,
# evaluate a self-defining term (X'..'/B'..'/C'..'), else report an error (via
# 'ctx') and return None.  'name' is for the message.  A SETC value used in an
# arithmetic (SETA) context is, per HLASM, evaluated as a single self-defining
# term
def coerceInt(value, name, ctx):
  if isinstance(value, int):
    return value
  if isinstance(value, str):
    if isSignedInt(value):
      return int(value)
    if isSelfDefiningTerm(value):
      return selfDefiningValue(value)
  ctx.error(f"Cannot be interpreted as an integer value: '{value}' ({name})")
  return None


# ---- arithmetic nodes ----
# Each arithmetic node knows how to evaluate itself via 'evalArith(self, ctx)',
# returning an int / 'Reloc' / float, or None on a (reported) error. 
@dataclass(frozen=True)
class Int:
  value: int

  def evalArith(self, ctx):
    return self.value


@dataclass(frozen=True)
class Loc:
  def evalArith(self, ctx):
    return ctx.star


@dataclass(frozen=True)
class Neg:
  operand: object

  def evalArith(self, ctx):
    v = self.operand.evalArith(ctx)
    return None if v is None else -v


@dataclass(frozen=True)
class BinOp:
  """A binary arithmetic operation, 'op' in '+ - * /', left-associative."""
  op: str
  left: object
  right: object

  def evalArith(self, ctx):
    left = self.left.evalArith(ctx)
    if left is None:
      return None
    right = self.right.evalArith(ctx)
    if right is None:
      ctx.error("Eval error type 2")
      return None
    op = self.op
    if op == "+":
      return left + right
    if op == "-":
      return left - right
    if op == "*":
      return left * right
    # A relocatable operand of '/' is illegal; yield a complex result so the
    # consuming site flags it (matching IEUF8V) r
    if isinstance(left, Reloc) or isinstance(right, Reloc):
      return Reloc(0, {POISON: 1})
    # SETA integer division truncates toward zero; /0 yields 0.
    if right == 0:
      return 0
    q = abs(left) // abs(right)
    return -q if (left < 0) != (right < 0) else q


@dataclass(frozen=True)
class Sym:
  name: str

  def evalArith(self, ctx):
    stored = ctx.svLocals.lookup(self.name)
    if stored is not None:
      return coerceInt(stored.value, self.name, ctx)
    if self.name == "*" and ctx.star is not None:
      return ctx.star
    return ctx.resolveLabel(self.name)


@dataclass(frozen=True)
class Var:
  """A macro/SET symbolic-variable reference '&X', with optional subscripts
  '&X(i)' / '&X(i,j)'.  'idxs' is a (possibly empty) tuple of arith index nodes
  -- a tuple, not a list, so the node stays hashable like the leaves above.
  """
  name: str
  idxs: tuple = ()

  def evalArith(self, ctx):
    stored = ctx.svLocals.lookup(self.name)
    if stored is None:
      ctx.error(f"Cannot find {self.name}")
      return None
    value, ok = ctx.applySubscripts(stored.value, self.idxs, self.name)
    return None if not ok else coerceInt(value, self.name, ctx)

  def evalBool(self, ctx):
    stored = ctx.svLocals.lookup(self.name)
    if stored is None:
      ctx.error(f"Not defined: {self.name}")
      return None
    value, ok = ctx.applySubscripts(stored.value, self.idxs, self.name)
    return value if ok else None

  def evalChar(self, ctx):
    stored = ctx.svLocals.lookup(self.name)
    if stored is None:
      ctx.error(f"Cannot evaluate string expression: {self.name}")
      return None
    value, ok = ctx.applySubscripts(stored.value, self.idxs, self.name)
    if not ok:
      return None
    if isinstance(value, bool):
      return "1" if value else "0"
    if isinstance(value, int):
      return str(value)
    return ctx.svReplace(value) if isinstance(value, str) else str(value)


@dataclass(frozen=True)
class Attr:
  """An attribute reference 'K'' / 'N'' / 'L'' of an 'operand' (a 'Sym' or
  'Var').  The T' type attribute is the separate char-context 'Tattr' node,
  not this one."""
  op: str
  operand: object

  def evalArith(self, ctx):
    op, operand = self.op, self.operand
    if op == "L":
      # Length attribute: operand is a bare symbol, or a SETC holding the name.
      if isinstance(operand, Sym):
        name = operand.name
      else:
        # Resolve the SETC variable to the symbol name it holds; if it is
        # defined in neither scope, fall back to the literal name.
        stored = ctx.svLocals.lookup(operand.name)
        name = stored.value if stored is not None else operand.name
      return ctx.lengthAttr(name)
    if not isinstance(operand, Var):
      ctx.error(f"{op}' requires a SET variable")
      return None
    stored = ctx.svLocals.lookup(operand.name)
    if stored is None:
      ctx.error(f"Symbolic variable {operand.name} not found")
      return None
    var, ok = ctx.applySubscripts(stored.value, operand.idxs, operand.name)
    if not ok:
      return None
    omitted = stored.omitted
    if op == "N":
      if operand.name != "&SYSLIST" and omitted is None:
        ctx.error(f"Not a macro parameter: {operand.name}")
        return None
      if operand.name != "&SYSLIST" and omitted:
        return 0
      if not isinstance(var, (tuple, list)):
        return 1
      return len(var)
    if op == "K":
      if isinstance(var, str):
        return len(var)
      if isinstance(var, bool):
        return 1
      if isinstance(var, int):
        return len(str(var))
      ctx.error(f"Not a string: {var!s}")
      return None
    ctx.error(f"Not yet implemented: {op}'")
    return None


def zRelocTarget(expr):
  """Split a Z-con reloc-target arith node into (symbol_name, addend).

  Handles a bare 'Sym' (addend 0) and 'Sym +/- <integer constant>'.  Returns
  (None, None) for an unsupported (complex) form, and (None, 0) for None.  Shared
  by the inline 'DC Z(...)' codegen and the '=Z(...)' literal path; the addend
  goes into the ZCON's HW0 (the linker adds the resolved symbol address)."""
  if expr is None:
    return None, 0
  if isinstance(expr, Sym):
    return expr.name, 0
  if isinstance(expr, BinOp) and expr.op in ("+", "-"):
    l, r = expr.left, expr.right
    if isinstance(l, Sym) and isinstance(r, Int):
      return l.name, (r.value if expr.op == "+" else -r.value)
    if isinstance(r, Sym) and isinstance(l, Int) and expr.op == "+":
      return r.name, l.value
  return None, None


@dataclass(frozen=True)
class Lcon:
  """An RS literal operand (=F'1', =X'AB', =Y(label), ...) -- what System/360
  calls an "lconstant".  'body' is the quoted body string for C/X/B/F/H/E/D, or
  the inner arithmetic node for Y; 'raw' is the Y inner's source text (literal-
  pool key / listing) and is None otherwise."""
  type: str
  body: object
  length: object = None
  scale: object = None
  raw: object = None
  flags: object = None      # =Z(...) flags operand (an arith node), else None

  def evalArith(self, ctx):
    # The numeric value of an RS literal.  C has no arithmetic value; a =Z
    # literal's "value" is its ADDEND (the in-pool HW0 pre-link), the symbol
    # being relocated by the linker rather than evaluated here.
    t, body = self.type, self.body
    if t == "Z":
      _name, addend = zRelocTarget(body)
      return addend if addend is not None else 0
    if t == "B":
      return int(body, 2)
    if t == "X":
      return int(body, 16)
    if t in ("E", "D"):
      return float(body)
    if t in ("F", "H"):
      return int(body) if body.isdigit() else float(body)
    if t == "Y":
      return body.evalArith(ctx)
    if t == "C":
      ctx.error("Cannot convert string '%s' to arithmetic expression" % body)
      return None
    ctx.error(f"Literals ={t} not implemented")
    return None


# ---- boolean nodes (conditional-assembly logic) ----
# Each evaluates itself via 'evalBool(self, ctx)', returning True / False or
# None on error
@dataclass(frozen=True)
class Bool:
  """A boolean literal (SETB 0 / 1)."""
  value: bool

  def evalBool(self, ctx):
    return self.value


@dataclass(frozen=True)
class Not:
  operand: object

  def evalBool(self, ctx):
    r = self.operand.evalBool(ctx)
    return None if r is None else (not r)


@dataclass(frozen=True)
class And:
  terms: tuple

  def evalBool(self, ctx):
    result = None
    for term in self.terms:
      v = term.evalBool(ctx)
      if v is None:
        ctx.error(f"Cannot evaluate boolean expression {term!s}")
        return None
      result = v if result is None else (result and v)
    return result


@dataclass(frozen=True)
class Or:
  terms: tuple

  def evalBool(self, ctx):
    result = None
    for term in self.terms:
      v = term.evalBool(ctx)
      if v is None:
        ctx.error(f"Cannot evaluate boolean expression {term!s}")
        return None
      result = v if result is None else (result or v)
    return result


@dataclass(frozen=True)
class Defined:
  """The 'D'sym' operator: True when the symbol is already defined.  'name' is
  a bare symbol/SETC string, or a 'Var' node for a (possibly subscripted)
  symbolic reference such as 'D'&SYSLIST(&I)' -- evaluated to the symbol it
  names before the defined-check."""
  name: object

  def evalBool(self, ctx):
    name = self.name
    if isinstance(name, str):
      sym = ctx.svReplace(name) if name.startswith("&") else name
    else:                                   # Var node (subscripted &SYSLIST etc.)
      sym = name.evalChar(ctx)
      if sym is None:
        return None
    return ctx.isDefined(sym)


@dataclass(frozen=True)
class Rel:
  """A relational comparison 'op' in EQ/NE/LT/GT/LE/GE of 'left' and 'right'
  (both arithmetic, or both character -- the evaluator decides by operand type)."""
  op: str
  left: object
  right: object

  def evalBool(self, ctx):
    left, right = self.left, self.right
    leftStr = isinstance(left, _CHAR_NODES)
    rightStr = isinstance(right, _CHAR_NODES)
    if bool(leftStr) != bool(rightStr):
      ctx.error("Cannot determine int vs char for comparison")
      return None
    if leftStr:
      vl = left.evalChar(ctx)
      vr = right.evalChar(ctx)
      if vl is None or vr is None:
        ctx.error(f"Cannot evaluate relational expression {self!s}")
        return None
      el = vl.encode("ebcdicvagc")
      er = vr.encode("ebcdicvagc")
      # DELIBERATE: string comparison is length-FIRST (a shorter string is
      # always less), not Python's lexicographic bytes ordering.
      if len(el) != len(er):
        cmp = -1 if len(el) < len(er) else 1
      else:
        cmp = (el > er) - (el < er)
    else:
      vl = left.evalArith(ctx)
      vr = right.evalArith(ctx)
      if vl is None or vr is None:
        ctx.error(f"Cannot evaluate relational expression {self!s}")
        return None
      cmp = (vl > vr) - (vl < vr)
    return {"EQ": cmp == 0, "NE": cmp != 0, "LT": cmp < 0,
            "LE": cmp <= 0, "GT": cmp > 0, "GE": cmp >= 0}[self.op]


# ---- character nodes ----
# Each evaluates itself to a string via 'evalChar(self, ctx)' (or None on a
# reported error), recursing via 'child.evalChar(ctx)' (and, for 'Substr',
# 'child.evalArith(ctx)') and deferring substitution to 'ctx.svReplace'.
@dataclass(frozen=True)
class Str:
  """A quoted string literal (apostrophes already un-doubled).  May still carry
  embedded '&X' references, substituted at eval time by 'svReplace'."""
  text: str

  def evalChar(self, ctx):
    return ctx.svReplace(self.text)


@dataclass(frozen=True)
class Concat:
  """Concatenation of two-or-more character 'parts' (a tuple)."""
  parts: tuple

  def evalChar(self, ctx):
    out = ""
    for part in self.parts:
      s = part.evalChar(ctx)
      if s is None:
        ctx.error(f"Cannot evaluate string expression: {part!s}")
        return None
      out += s
    return out


@dataclass(frozen=True)
class Substr:
  """A substring ''s'(start,length)' of a character 'source', with arith
  'start' (1-based) and 'length'."""
  source: object
  start: object
  length: object

  def evalChar(self, ctx):
    s = self.source.evalChar(ctx)
    if s is None:
      return None
    i = self.start.evalArith(ctx)
    n = self.length.evalArith(ctx)
    if i is None or n is None:
      ctx.error("Cannot evaluate substring index/length")
      return None
    i -= 1
    if i < 0 or n < 0:
      ctx.error("Index or length out of range")
      return None
    return s[i:i + n]


def _symbolType(value, ctx):
  """The T' type attribute of a non-empty operand 'value' (a string): 'N' for a
  self-defining term or a (optionally signed) integer, the type recorded by a
  3-operand EQU (e.g. '#'/'@' for PDEF position symbols) if any, else the 'C'
  legacy default.  Signed integers count as numeric to match the assembler's
  arithmetic coercion ('isSignedInt'): the #SPLIT macro strips one sign from a
  doubly-signed value like '--82' and tests 'T'' of the '-82' remainder."""
  if isSelfDefiningTerm(value) or isSignedInt(value):
    return "N"
  recorded = ctx.typeAttr(value)
  return recorded if recorded is not None else "C"


@dataclass(frozen=True)
class Tattr:
  """The 'T'' type attribute of an 'operand' (a 'Sym' or 'Var'): 'O' for an
  omitted/null operand, 'N' for a self-defining term, an EQU-declared type
  attribute, else 'C'."""
  operand: object

  def evalChar(self, ctx):
    operand = self.operand
    if isinstance(operand, Sym):
      name = ctx.svReplace(operand.name)
      return "O" if name == "" else _symbolType(name, ctx)
    name, idxs = operand.name, operand.idxs      # operand is a Var
    stored = ctx.svLocals.lookup(name)
    if stored is not None:
      value = stored.value
    elif not idxs:
      # An undefined bare &X: svReplace leaves the literal text ("&X"), which
      # is non-empty -> type attribute 'C' (matches the legacy T'identifier).
      name = ctx.svReplace(name)
      return "O" if name == "" else _symbolType(name, ctx)
    else:
      ctx.error(f"T' of unknown symbol {name}")
      return None
    value, ok = ctx.applySubscripts(value, idxs, name)
    if not ok:
      return None
    if isinstance(value, str):
      return "O" if value == "" else _symbolType(value, ctx)
    return "C"


# The character-valued node types.  The 'rel' evaluator uses these to decide
# whether a comparison is character (both operands string-typed) or arithmetic.
# A 'Var' is deliberately NOT here: a SETC/SETA/SETB variable is classed as
# arithmetic in a comparison (matching the legacy tuple behaviour, where "var"
# was never a string tag).
_CHAR_NODES = (Str, Concat, Substr, Tattr)
