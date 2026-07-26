#!/usr/bin/env python3
#
# Lark-based front-end for the asm101 assembler: a grammar
# plus a transformer that emits an AST.
# 
import os
from dataclasses import dataclass, field
from functools import cache
from lark import Lark, Transformer, v_args
from ap101Utils import codepages  # noqa: F401 -- registers the 'ebcdicvagc' codec
from .astnodes import (
    Int, Loc, Neg, BinOp, Sym, Var, Attr, Lcon,
    Bool, Not, And, Or, Defined, Rel, Str, Concat, Substr, Tattr,
    ebcdicValue as _ebcdic,
)

_GRAMMAR = os.path.join(os.path.dirname(__file__), "asm.lark")


@dataclass
class Aif:
  """AIF directive: branch to the sequence symbol 'target' when the boolean
  expression node 'cond' evaluates true."""
  cond: object
  target: str


@dataclass
class Equ:
  """EQU operand: the value expression 'v', an optional 'length' modifier
  expression, and an optional 'tattr' (the 3rd 'type attribute' operand -- a
  character self-defining term whose value names the symbol's T' type, e.g.
  PDEF emits 'EQU 1,1,C'#'' to give a symbol type attribute '#')."""
  v: object
  length: object = None
  tattr: object = None


@dataclass
class Mnote:
  """MNOTE directive carrying a diagnostic message 'msg'.  'comment' is True for
  the 'MNOTE *,'..'' form (a listing comment, no error raised); otherwise
  'severity' is the explicit level, or None for the bare 'MNOTE '..'' form that
  defaults to severity 1."""
  msg: str
  severity: int | None = None
  comment: bool = False


@dataclass
class MacroText:
  """A scalar macro-invocation argument value -- a bare token, a quoted string,
  or a NUMID -- whose 'text' is used verbatim both as the argument value and as
  a sublist element."""
  text: str
  def asValue(self):   return self.text
  def asElement(self): return self.text


@dataclass
class MacroSublist:
  """A parenthesised macro-argument value '(e1,e2,...)'; 'items' are MacroText /
  MacroSublist.  As a top-level argument value it renders to the tuple of its
  elements; nested in another sublist it re-renders to the source '(e1,...,eN)'
  string so '&P' / '&P(k)' round-trip on re-parse."""
  items: list
  def asValue(self):   return tuple(it.asElement() for it in self.items)
  def asElement(self): return "(" + ",".join(it.asElement() for it in self.items) + ")"


@dataclass
class MacroArg:
  """One macro-invocation argument.  'name' is the formal "&NAME" for a keyword
  argument or None for a positional one; 'value' is its MacroText / MacroSublist.
  'macroArg()' yields the (name, replacement) pair the expander binds -- the
  replacement being a string (scalar) or a tuple of strings (sublist)."""
  name: object        # "&NAME" or None
  value: object
  def macroArg(self):
    return self.name, self.value.asValue()


@dataclass
class DcSuboperand:
  """DC/DS suboperand:
    type    the type letter (C/X/B/F/H/E/D/A/Y/Z)
    length  the ('L', ...) length-modifier tuple (parseDcLength input), or None
    values  value leaves: a string for C/X/B, float-number strings for F/H/E/D,
            or clean arith nodes for A/Y
    hexstr  the A-hex form's hex string, or None
    zexpr   the Z reloc-target arith node (a bare Sym, or Sym +/- a constant
            addend), or None
    zdata   True when the Z reloc target is the DATA address subfield
            ('Z(,sym...)' -- code subfield empty), which relocates as
            ZCON/data (RLD 0x50) instead of ZCON/code (0x04)
    flags   the Z flags arith node, or None
    scale   the fixed-point scale modifier (Sn) integer for F/H/E/D, or None
    dup     duplication-factor arith node, or None (filled in by 'dcsub')"""
  type: str
  length: object = None
  values: list = field(default_factory=list)
  hexstr: object = None
  zexpr: object = None
  zdata: bool = False
  flags: object = None
  scale: object = None
  dup: object = None


@v_args(inline=True)
class _AST(Transformer):
  def __init__(self, text=""):
    super().__init__()
    self._text = text          # the (comment-stripped) source, for raw spans

  # ---- arithmetic ----
  def num(self, tok):       return Int(int(tok))
  def hexlit(self, tok):    return Int(int(str(tok)[2:-1], 16))
  def binlit(self, tok):    return Int(int(str(tok)[2:-1], 2))
  def charlit(self, tok):   return Int(_ebcdic(str(tok)[2:-1]))
  def loc(self, tok):       return Loc()
  def sym(self, tok):       return Sym(str(tok))

  def mvar(self, name, sub=None):
    return Var(str(name), tuple(sub) if sub is not None else ())

  def subscript(self, *exprs):
    return list(exprs)

  def attr(self, pfx, operand):
    if isinstance(operand, str):
      operand = Sym(str(operand))
    return Attr(str(pfx)[0], operand)

  def binop(self, left, op, right):
    return BinOp(str(op), left, right)

  def unary(self, op, node):
    return Neg(node) if str(op) == "-" else node

  # ---- boolean ----
  def boollit(self, tok):   return Bool(str(tok) == "1")
  def notop(self, _kw, node):  return Not(node)
  def defined(self, _pfx, name):
    # 'name' is a bare SYMBOL token (str subclass) or a 'Var' node from 'mvar'
    # (carrying any subscript, e.g. D'&SYSLIST(&I)).
    return Defined(name if isinstance(name, Var) else str(name))

  def orop(self, left, _kw, right):
    terms = left.terms if isinstance(left, Or) else (left,)
    return Or(terms + (right,))

  def andop(self, left, _kw, right):
    terms = left.terms if isinstance(left, And) else (left,)
    return And(terms + (right,))

  def arith_rel(self, left, op, right):
    return Rel(str(op), left, right)

  def char_rel(self, left, op, right):
    return Rel(str(op), left, right)

  # ---- character ----
  def concat(self, *parts):
    parts = [p for p in parts if p is not None]
    return parts[0] if len(parts) == 1 else Concat(tuple(parts))

  def strlit(self, tok, sub=None):
    s = str(tok)[1:-1].replace("''", "'")
    if sub is None:
      return Str(s)
    start, length = sub
    return Substr(Str(s), start, length)

  def substring(self, start, length):
    return (start, length)

  def tattr(self, _pfx, operand):
    if isinstance(operand, str):          # a bare SYMBOL token (str subclass)
      operand = Sym(str(operand))
    return Tattr(operand)

  # DOT join markers are dropped (filtered in concat); keep the token out.
  def DOT(self, tok):
    return None

  # ---- operand formats ----
  # Each returns a dict keyed by the subfield names model101 reads, holding a
  # clean expression, present only when that subfield was supplied.
  def rr(self, *args):
    if len(args) == 1:
      return {"R2": args[0]}
    return {"R1": args[0], "R2": args[1]}

  def ri(self, r2, i1):
    return {"R2": r2, "I1": i1}

  def si_based(self, d2, b2, i1):
    return {"D2": d2, "B2": b2, "I1": i1}

  def si_plain(self, d2, i1):
    return {"D2": d2, "I1": i1}

  # rs: each alternative carries an optional leading R1 (arith ",").  Peel
  # it off the front; the remaining args are the tail fields.
  @staticmethod
  def _r1(r1list, fields):
    if r1list:
      fields["R1"] = r1list[0]
    return fields

  def rs_lit(self, *a):                 # [R1,] =lconstant
    return self._r1(a[:-1], a[-1])    # a[-1] is the {"L2": ...} dict

  def rs_d2(self, *a):                  # [R1,] D2
    *r1, d2 = a
    return self._r1(r1, {"D2": d2})

  def rs_d2_b2(self, *a):               # [R1,] D2(B2)
    *r1, d2, b2 = a
    return self._r1(r1, {"D2": d2, "B2": b2})

  def rs_d2_x2_b2(self, *a):            # [R1,] D2(X2,B2)
    *r1, d2, x2, b2 = a
    return self._r1(r1, {"D2": d2, "X2": x2, "B2": b2})

  def rs_d2_b2only(self, *a):           # [R1,] D2(,B2)  -- X2 omitted
    # B2ONLY marks the comma form: 'expr(,Rn)' (explicitly omitted
    # index) requests the INDEXED RS AM=1 format and never condenses to
    # SRS, unlike the bare 'expr(Rn)' base form.
    *r1, d2, b2 = a
    return self._r1(r1, {"D2": d2, "B2": b2, "B2ONLY": True})

  def rs_d2_empty(self, *a):            # [R1,] D2()
    *r1, d2 = a
    return self._r1(r1, {"D2": d2})

  # ---- lconstant (RS literal) ----
  # The clean node is an 'Lcon' dataclass (see above): 'body' is the quoted
  # body string (escapes preserved) for C/X/B/F/H/E/D, or the inner arith
  # expression for Y; 'raw' is the Y inner's source text (pool key / listing).
  def lcon_quoted(self, littype, *rest):
    t = str(littype)[1]               # char after '='
    length, scale = self._modifiers(rest[:-1])
    body = str(rest[-1])[1:-1]        # strip the quotes; KEEP '' escapes
    return {"L2": Lcon(t, body, length, scale)}

  @v_args(meta=True, inline=True)
  def lcon_y(self, meta, lity, *rest):
    length, scale = self._modifiers(rest[:-1])
    expr = rest[-1]
    node = self._text[meta.start_pos:meta.end_pos]
    raw = node[node.index("(") + 1:node.rindex(")")]
    return {"L2": Lcon("Y", expr, length, None, raw)}

  def _zparts(self, meta, rest):
    """Shared by the Z-form rules: (non-DCLEN arith nodes, parenthesized
    source text).  First arith = reloc target, last = flags where present;
    any middle arith (length/base) is dropped."""
    ariths = [a for a in rest if getattr(a, "type", None) != "DCLEN"]
    node = self._text[meta.start_pos:meta.end_pos]
    raw = node[node.index("(") + 1:node.rindex(")")]
    return ariths, raw

  # =Z literal: same operand shape as 'DC Z(...)'.  'body' holds the
  # reloc-target arith; 'raw' is the parenthesized source (pool key /
  # listing).
  @v_args(meta=True, inline=True)
  def lcon_z(self, meta, litz, *rest):
    ariths, raw = self._zparts(meta, rest)
    return {"L2": Lcon("Z", ariths[0] if ariths else None, None, None, raw)}

  @v_args(meta=True, inline=True)
  def lcon_z_flags(self, meta, litz, *rest):
    ariths, raw = self._zparts(meta, rest)
    return {"L2": Lcon("Z", ariths[0], None, None, raw, ariths[-1])}

  # =Z(,tgt[,flags]): empty code subfield -- the DATA address subfield is the
  # reloc target (ZCON/data RLD 0x50; the raw pool key keeps the comma, so
  # data- and code-subfield literals never unify).
  @v_args(meta=True, inline=True)
  def lcon_z_data(self, meta, litz, *rest):
    ariths, raw = self._zparts(meta, rest)
    return {"L2": Lcon("Z", ariths[0] if ariths else None, None, None, raw,
                       None, True)}

  @v_args(meta=True, inline=True)
  def lcon_z_data_flags(self, meta, litz, *rest):
    ariths, raw = self._zparts(meta, rest)
    return {"L2": Lcon("Z", ariths[0], None, None, raw, ariths[-1], True)}

  def equ(self, *a):                    # EQU: value [, length [, type]]
    return Equ(a[0],
               a[1] if len(a) >= 2 else None,
               a[2] if len(a) >= 3 else None)

  def exprs(self, *a):                   # USING / DROP: a list of values
    return {"r": list(a)}

  def idlist(self, *syms):               # ENTRY / EXTRN: a list of symbols
    return [str(s) for s in syms]

  def aif(self, cond, seq):              # AIF: (cond).target
    return Aif(cond, str(seq))

  # ---- MNOTE ----
  # an 'Mnote' dataclass (see above).  The message keeps '' un-doubled 
  @staticmethod
  def _mnote_str(tok):
    return str(tok)[1:-1].replace("''", "'")

  def mnote_sev(self, num, s):
    return Mnote(self._mnote_str(s), severity=int(num))

  def mnote_com(self, s):
    return Mnote(self._mnote_str(s), comment=True)

  def mnote_msg(self, s):
    return Mnote(self._mnote_str(s))

  # ---- IOP (MSC / BCE) operands ----
  # {"ops": [expr, ...]} in source order, plus "index": expr when
  # a trailing "(N)" index flag is present.
  def msc_named(self, c0, addr, idx=None):   # constant "," arith ["(" c ")"]
    # 'addr' is an arith node: a 'Sym' for a named target (@LBP 6,BINIT2), or a
    # numeric value for a literal address (@LBP 11,0 -- the macro-generated
    # "no PC" sentinel).
    d = {"ops": [c0, addr]}
    if idx is not None:
      d["index"] = idx
    return d

  def msc_expr(self, a, idx=None):           # arith ["(" const ")"]
    d = {"ops": [a]}
    if idx is not None:
      d["index"] = idx
    return d

  def bce_idx(self, a, idx):                 # arith "(" const ")"
    return {"ops": [a], "index": idx}

  def bce_two(self, a, b):                   # arith "," arith
    return {"ops": [a, b]}

  def bce_one(self, a):                      # arith
    return {"ops": [a]}

  # ---- macro prototype formals ----
  # {"pi": [...]} where each item is "&name" (bare) or ("&name","=",default).
  def oproto(self, *params):
    return {"pi": list(params)}

  def oparam_bare(self, mvar):
    return str(mvar)

  def oparam_kw(self, mvar, val=None):
    return (str(mvar), "=", str(val) if val is not None else "")

  # ---- macro invocation replacements ----
  # Each argument is a 'MAcroArg' (positional name=None, or keyword "&NAME"); its
  # value is a 'MacroText' (scalar) or 'MacroSublist'.  '_eval_macro_argument'
  # just calls 'MacroArg.macroArg()'.
  def oinv(self, *repls):
    return {"pi": list(repls)}

  def kw_list(self, sym, lst):              # id "=" "(" list ")"
    return MacroArg("&" + str(sym), MacroSublist(lst))

  def kw_qstr(self, sym, s):                # id "=" quoted string (may hold spaces)
    return MacroArg("&" + str(sym), MacroText(str(s)))

  def kw_bare(self, sym, val=None):         # id "=" /[^, ()]*/
    return MacroArg("&" + str(sym),
                    MacroText(str(val) if val is not None else ""))

  def pos_qstr(self, s):                    # quoted string (kept verbatim, '' and all)
    return MacroArg(None, MacroText(str(s)))

  def pos_list(self, lst):                  # "(" list ")"
    return MacroArg(None, MacroSublist(lst))

  def pos_numid(self, a, b):                # name "(" name ")"
    return MacroArg(None, MacroText(f"{a}({b})"))

  def pos_bare(self, val=None):             # /[^, ()]*/  (possibly empty)
    return MacroArg(None, MacroText(str(val) if val is not None else ""))

  # sublist -> a flat list of MacroText / MacroSublist items
  def rlist(self, first, *rest):
    return [first, *rest]

  def litem_sub(self, lst):
    return MacroSublist(lst)

  @v_args(meta=True, inline=True)
  def litem_bare(self, meta, *_kids):
    # The bare item's raw source span (includes any absorbed "(subscript)").
    # An empty item (between commas, e.g. the trailing ",," of a sublist)
    # has no source position -> the empty string.
    if getattr(meta, "empty", False):
      return MacroText("")
    return MacroText(self._text[meta.start_pos:meta.end_pos])

  def nameset(self, sv, *exps):          # SET name: &var [(exp[,exp])]
    d = {"sv": str(sv)}
    if len(exps) >= 1:
      d["exp"] = exps[0]
    if len(exps) >= 2:
      d["exp2"] = exps[1]
    return d

  # ---- DC / DS suboperands ----
  # Each dc_* rule emits a clean suboperand dict keyed by the field names
  # model101 reads (t, l, v, z, f, h); dcsub adds the duplication factor (d).
  # The value fields hold *clean* leaves: a bare string for C/X/B (escapes
  # preserved), a list of float-number strings for F/H/E/D, and a list of
  # clean arith nodes for the A/Y address forms.  The length modifier (l) is
  # kept as the legacy ('L', ...) tuple that parseDcLength consumes.
  @staticmethod
  def _dclen(tok):
    # "L4" -> ('L','4');  "L.16" -> ('L','.','16');  "L-5" -> ('L','-','5')
    s = str(tok)[1:]                  # drop the leading 'L'
    elems = ["L"]
    if s and s[0] == ".":
      elems.append(".")
      s = s[1:]
    if s and s[0] in "+-":
      elems.append(s[0])
      s = s[1:]
    elems.append(s)
    return tuple(elems)

  @staticmethod
  def _split_len_str(args):
    """Pull an optional DCLEN token and an optional STRING token out of a
    dc_c/dc_x/dc_b/dc_float argument list (after any leading type token has
    been removed).  Returns (lengthTupleOrNone, valueOrNone)."""
    length = None
    value = None
    for a in args:
      if getattr(a, "type", None) == "DCLEN":
        length = _AST._dclen(a)
      else:                          # STRING token
        value = str(a)[1:-1]       # strip quotes; KEEP '' and && escapes
    return length, value

  def dc_c(self, *args):
    length, v = self._split_len_str(args)
    return DcSuboperand("C", length, [v] if v is not None else [])

  def dc_x(self, *args):
    length, v = self._split_len_str(args)
    if v is not None: # X constants sometimes include ',' separators;
      v = v.replace(",", "")
    return DcSuboperand("X", length, [v] if v is not None else [])

  def dc_b(self, *args):
    length, v = self._split_len_str(args)
    return DcSuboperand("B", length, [v] if v is not None else [])

  def dc_float(self, ttok, *args):
    # Pull out an optional scale modifier (Sn / S-n):
    scale = None
    rest = []
    for a in args:
      if getattr(a, "type", None) == "SMOD":
        scale = int(str(a)[1:])       # strip leading 'S'
      else:
        rest.append(a)
    length, body = self._split_len_str(rest)
    return DcSuboperand(str(ttok), length,
                        body.split(",") if body is not None else [],
                        scale=scale)

  @staticmethod
  def _split_len_exprs(args):
    length = None
    exprs = []
    for a in args:
      if getattr(a, "type", None) == "DCLEN":
        length = _AST._dclen(a)
      else:
        exprs.append(a)
    return length, exprs

  def dc_a(self, *args):
    length, exprs = self._split_len_exprs(args)
    return DcSuboperand("A", length, exprs)

  def dc_y(self, *args):
    length, exprs = self._split_len_exprs(args)
    return DcSuboperand("Y", length, exprs)

  def dc_ahex(self, *args):
    length, h = self._split_len_str(args)
    return DcSuboperand("A", length, hexstr=h)

  def dc_z(self, *args):                   # Z(,tgt) | Z(tgt) | Z(tgt,mid) | bare Z
    length, zexpr = None, None
    ariths = []
    for a in args:
      if getattr(a, "type", None) == "DCLEN":
        length = _AST._dclen(a)
      else:
        ariths.append(a)
    if ariths:
      zexpr = ariths[0]
    return DcSuboperand("Z", length=length, zexpr=zexpr)

  def dc_z_flags(self, *args):            # ...,flags  (the trailing arith)
    # First arith = reloc target, last arith = flags, any middle (the ignored
    # base/length field) is dropped.  Works for both 'Z(tgt,flags)' (2 args)
    # and 'Z(tgt,base,flags)'.
    ariths = [a for a in args if getattr(a, "type", None) != "DCLEN"]
    return DcSuboperand("Z", zexpr=ariths[0], flags=ariths[-1])

  def dc_z_data(self, *args):             # Z(,tgt) -- DATA-subfield target
    ariths = [a for a in args if getattr(a, "type", None) != "DCLEN"]
    return DcSuboperand("Z", zexpr=ariths[0] if ariths else None, zdata=True)

  def dc_z_data_flags(self, *args):       # Z(,tgt,flags)
    ariths = [a for a in args if getattr(a, "type", None) != "DCLEN"]
    return DcSuboperand("Z", zexpr=ariths[0], zdata=True, flags=ariths[-1])

  def dup(self, x):
    # 'x' is a raw integer token for a bare factor ('3DC...'), or an already-
    # transformed arith node for a parenthesized '(expr)' factor. 
    return Int(int(x)) if isinstance(x, str) else x

  def dcsub(self, *args):
    if len(args) == 2:
      dupval, sub = args
      sub.dup = dupval
    else:
      sub = args[0]
    return sub

  def dc(self, *subs):
    return list(subs)

  ds = dc

  @staticmethod
  def _modifiers(toks):
    length = scale = None
    for tok in toks:
      s = str(tok)
      if s[0] == "L":
        length = int(s[1:])
      elif s[0] == "S":
        scale = int(s[1:])
    return length, scale


# Start rules that are LALR(1) under the contextual lexer.
# Not LALR:
#   - bool/aif
#     - needs the arith-vs-character RELOP relation (a reduce/reduce conflict the
#       contextual lexer cannot resolve)
#   - oinv/oproto's catch-all 
#     - REPLVAL/PROTOVAL terminals tokenize differently LALR than Earley.  
#
_LALR_STARTS = frozenset({
    "arith", "arith_only", "cexpr", "rr", "ri", "si", "rs", "equ",
    "exprs", "idlist", "nameset", "mnote", "msc", "bce", "dc", "ds",
})


# Starts whose transformer rules read node positions (rs: lcon_y/lcon_z*;
# oinv: litem_bare).  The rest skip per-node position bookkeeping.
_META_STARTS = frozenset({"rs", "oinv"})


@cache
def _parser(start):
  if start == "ds":
    return _parser("dc")   # identical grammar rule; share one parser
  isLalr = start in _LALR_STARTS
  return Lark.open(_GRAMMAR, start=start,
                   parser = "lalr" if isLalr else "earley",
                   lexer = "contextual" if isLalr else "auto",
                   maybe_placeholders=False,
                   propagate_positions=(start in _META_STARTS))


# Quote/paren/attribute-aware field scanning, shared by the Stage-0 comment
# strip ('_strip_comment'), cardreader's macro-operand-end probe
# ('first_blank_outside'), and the SET sub-operand splitter ('split_top_level').
# The subtlety is the OVERLOADED apostrophe: it opens a quoted string (C'..',
# X'..', B'..', or a bare '..'), BUT the attribute operators N'/K'/L'/S'/I'/T'
# use an apostrophe that is NOT a string quote (e.g. K'&STR -- the operand
# follows unquoted).  An apostrophe is an attribute operator iff it directly
# follows a STANDALONE attribute letter -- one not itself preceded by a letter,
# so a symbol ending in K ("MASK'") is a quote, not a K' attribute.  'D' is the
# extra wrinkle: D'SYMBOL is the defined attribute, but D'1.5' is a D-type float
# constant whose apostrophe quotes -- so D' counts as an attribute only when a
# symbol start (letter / & # $ @) follows, not a float's digit / sign / dot.
def _attr_apostrophe(text, i, in_quote):
  """True if the apostrophe at text[i] is an attribute operator, not a quote."""
  if in_quote or i < 1 or text[i - 1] not in "KLTNISD":
    return False
  if i >= 2 and text[i - 2].isalpha():
    return False
  if text[i - 1] == "D":
    nxt = text[i + 1] if i + 1 < len(text) else ""
    return nxt[:1].isalpha() or nxt[:1] in "&#$@"
  return True


def first_blank_outside(text):
  """Index of the first blank outside quotes and parentheses (the start of a
  trailing comment), or None if there is none."""
  quoted = False
  depth = 0
  for i, ch in enumerate(text):
    if ch == "'":
      if not _attr_apostrophe(text, i, quoted):
        quoted = not quoted
    elif not quoted:
      if ch == " " and depth == 0:
        return i
      elif ch == "(":
        depth += 1
      elif ch == ")" and depth > 0:
        depth -= 1
  return None


def split_top_level(text):
  """Split 'text' at top-level commas (outside quotes and parentheses), stopping
  at the trailing comment (the first top-level blank).  A comma inside a quoted
  string, a parenthesised list, or an attribute subscript stays in one element."""
  parts = []
  quoted = False
  depth = 0
  start = 0
  for i, ch in enumerate(text):
    if ch == "'":
      if not _attr_apostrophe(text, i, quoted):
        quoted = not quoted
    elif not quoted:
      if ch == " " and depth == 0:
        return parts + [text[start:i]]
      elif ch == "(":
        depth += 1
      elif ch == ")" and depth > 0:
        depth -= 1
      elif ch == "," and depth == 0:
        parts.append(text[start:i])
        start = i + 1
  return parts + [text[start:]]


def _strip_comment(text):
  i = first_blank_outside(text)
  return text if i is None else text[:i]


# Rules whose field is the *whole* operand text, so Stage-0 comment stripping is
# skipped:
NO_STRIP = frozenset({
    "nameset", "arith_only", "bool_only", "cexpr_only",
})


@cache
def parse(text, rule):
  parser = _parser(rule)
  stripped = text if rule in NO_STRIP else _strip_comment(text)
  try:
    tree = parser.parse(stripped)
    return _AST(stripped).transform(tree)
  except Exception:
    return None
