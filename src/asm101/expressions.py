#!/usr/bin/env python3
#
# Macro expression evaluation for the AP-101 assembler
# 
# This file is based on ASM101S/expressions.py from virtualagc by Ronald Burkey:
#   https://github.com/virtualagc/virtualagc/blob/master/ASM101S/expressions.py
# 
# It's been significantly refactored for asm101.  A summary of major changes:
# 

import re
from collections import namedtuple
from dataclasses import dataclass, field
from typing import Optional
from .larkparse import parse, split_top_level
# The expression nodes now evaluate themselves (see `astnodes.py`), so this
# module no longer references the node classes directly.  `Reloc` -- the
# relocatable-value type a `Sym` resolves to -- is still used here (and
# re-exported, since model101 imports it from `expressions`).
from .astnodes import Reloc
from .statement import Statement

# Already-defined normal program labels (e.g. `MYSYM`), as opposed to macro-
# language symbolic variables or sequence symbols.  Currently only used to
# implement the `D'` operator.
definedNormalSymbols = {}

@dataclass
class SymbolicVar:
  value: object
  omitted: Optional[bool] = None


class SymbolTable(dict):
  def __init__(self, *args, parent=None, **kwargs):
    super().__init__(*args, **kwargs)
    self.parent = parent
    self.declaredGlobals = set()

  def lookup(self, name):
    if name in self:
      return self[name]
    if self.parent is not None:
      return self.parent.lookup(name)
    return None

  def declareGlobal(self, name):
    self.declaredGlobals.add(name)

  def lookupForSet(self, name):
    if name in self:
      return self
    if (name in self.declaredGlobals
            and self.parent is not None and name in self.parent):
      return self.parent
    return None


svGlobals = SymbolTable()
# Shared empty scope for evaluation with no additional locals:
NO_LOCALS = SymbolTable(parent=svGlobals)
# "Local" variables in the global context: defined at top-level scope but not
# accessible at any lower scope level.
svGlobalLocals = SymbolTable({"parent": [None, 0, 0, None]}, parent=svGlobals)


@dataclass
class AsmContext:
  passCount: int = -1  # -1->front-end/macro, 0..N during codegen
  firstCSECT: Optional[str] = None
  symbolAttributes: dict = field(default_factory=dict)
  symbols: Optional["SymbolTable"] = field(default=None, repr=False)

  def reset(self):
    self.passCount = -1
    self.symbolAttributes = {}


asmContext = AsmContext(symbols=svGlobals)  # Use as `global` alongside svGlobals.

# Mark an error in the array of source code.
errorCount = 0
maxSeverity = 0


class Diagnostic(namedtuple("Diagnostic", "passCount severity message")):
  __slots__ = ()

  def __str__(self):
    return f"(Pass {self.passCount}, Severity {self.severity}) {self.message}"


def error(stmt, msg, severity=255):
  global errorCount, maxSeverity
  if severity > maxSeverity:
    maxSeverity = severity
  errorCount += 1
  stmt.errors.append(Diagnostic(asmContext.passCount, severity, msg))
def getErrorCount():
  return errorCount, maxSeverity

# Apply a 1-based subscript `iv` to `value` with SET/SYSLIST sublist semantics:
# a scalar behaves as a one-element sublist, and any out-of-range index yields
# the null string "" (macros walk operands/sublists with `AIF ('&P(k)' NE '')`
# and rely on a past-the-end reference going null rather than erroring).  The
# caller handles the &SYSLIST(0)=macro-name special case before calling this.
def indexValue(value, iv, depth=0):
  iv -= 1  # 1-based -> 0-based
  if not isinstance(value, (tuple, list)):
    # A nested sublist is stored as its source text "(a,b,...)" (a sublist
    # element re-renders to a string).  Once already inside a sublist
    # (depth >= 1), a further subscript descends into it -- e.g.
    # &SYSLIST(i,2,1) reads element 1 of the nested sublist at (i,2).  At
    # depth 0 a bare scalar stays a one-element sublist, so a parenthesised
    # SETC value is not torn apart.
    if depth >= 1 and isinstance(value, str) and len(value) >= 2 \
            and value[0] == "(" and value[-1] == ")":
      sub = split_top_level(value[1:-1])
      return sub[iv] if 0 <= iv < len(sub) else ""
    return value if iv == 0 else ""
  if iv < 0 or iv >= len(value):
    return ""
  return value[iv]

# N' of a single operand value.  A nested sublist is stored re-rendered as its
# source text "(a,b,...)" (see indexValue), so N' must count its top-level
# elements; any other scalar counts 1.
def sublistCount(value):
  if isinstance(value, str) and len(value) >= 2 \
          and value[0] == "(" and value[-1] == ")":
    return len(split_top_level(value[1:-1]))
  return 1

'''
`svReplace` replaces all symbolic variables (e.g. &A) given by `svGlobals` and
`svLocals` in a string.  `SV_PATTERN` matches any symbolic variable, defined or
not.  The subtlety: a match may be a bare &A or an indexed &A(expression), so an
index reference must be parsed before the value can be substituted.
'''
SV_PATTERN = re.compile("(?<!&)&[A-Z#$@][A-Z#$@0-9]*(?![#@_$A-Z0-9])")
def _parseSubscriptRef(text, nameEnd):
  """Parse a "(exp[,exp[,exp]])" subscript beginning at text[nameEnd] -- just
  past a &VAR name -- and return (exprs, end): a list of 1-3 parsed arith index
  ASTs and the text index just past the closing ')'.  Three levels reach a
  nested sublist's element (DOPROC's `&SYSLIST(&I,2,1)`).

  Returns ([], nameEnd) -- a BARE reference, parenthesized text left in place --
  when there is no balanced subscript of one-to-three valid arith expressions.
  Inner expressions parse via `arith_only`."""
  if nameEnd >= len(text) or text[nameEnd] != "(":
    return [], nameEnd
  depth = 0
  commas = []
  close = -1
  for i in range(nameEnd, len(text)):
    ch = text[i]
    if ch == "(":
      depth += 1
    elif ch == ")":
      depth -= 1
      if depth == 0:
        close = i
        break
    elif ch == "," and depth == 1:
      commas.append(i)
  if close < 0 or len(commas) > 2:        # unbalanced, or more than three indices
    return [], nameEnd
  bounds = [nameEnd] + commas + [close]
  parts = [text[bounds[k] + 1:bounds[k + 1]] for k in range(len(bounds) - 1)]
  exprs = []
  for p in parts:
    a = parse(p, "arith_only")
    if a is None:                       # not a valid arith index -> bare
      return [], nameEnd
    exprs.append(a)
  return exprs, close + 1


def svReplace(stmt, text, svLocals):
  global svGlobals
    
  if "&" not in text: # no vars: Early out
    return text
    
  # Replace in reverse order (end to start) so indexes of not-yet-replaced
  # matches don't shift.  `originalText` is a snapshot: the per-match
  # "does an index follow?" test must read the ORIGINAL field, because a
  # rightward neighbour may already have been replaced.  Otherwise adjacent
  # variables like "&A&B" mis-parse -- &B's replacement gets absorbed into &A's
  # name, leaving every other concatenated variable unsubstituted.
  originalText = text
  matches = []
  for match in SV_PATTERN.finditer(text):
    matches.append(match)
  for match in reversed(matches):
    sv = match.group()
    start = match.span()[0]
    end = match.span()[1]
    # Do not substitute the operand of a type-attribute reference (T'&X or
    # T'&X(i)): T' must be evaluated against the symbol itself.  Substituting
    # its value would destroy the reference, and an omitted operand would leave
    # a dangling "T'" that fails to parse -- exactly what macros test with
    # "AIF (T'&P EQ 'O')".  (K'/N' are handled below, so only T' is skipped.)
    if start >= 2 and text[start-2:start] == "T'":
      continue
    # The finditer match is the bare variable name (regex stops before any
    # "(index)"); detect a following index against the ORIGINAL text (a
    # rightward neighbour may already have been replaced).  Parse that
    # subscript ourselves to get both the index AST(s) and the end offset.
    exps = []
    if end < len(originalText) and originalText[end] == "(":
      exps, end = _parseSubscriptRef(text, end)
    stored = svLocals.lookup(sv)
    if stored is None:
      continue
    replacement = stored.value
    if exps:
      if len(exps) >= 2:
        # Multi-subscript &X(i,j[,k]): operand i, element j of its sublist,
        # then element k of THAT nested sublist (e.g. DOPROC's
        # `LR &SYSLIST(&I,1),&SYSLIST(&I,2)` and `&SYSLIST(&I,2,1)`).
        # Semantics: &SYSLIST(0) is the macro name, a scalar is a one-element
        # sublist, an out-of-range index is null.
        evaled = []
        bad = False
        for ix in exps:
          iv = evalArithmeticExpression(ix, svLocals, stmt)
          if iv == None:
            error(stmt, f"Cannot evaluate index of {sv}: {ix!s}")
            bad = True
            break
          evaled.append(iv)
        if bad:
          continue
        for depth, iv in enumerate(evaled):
          if sv == "&SYSLIST" and depth == 0 and iv == 0:
            replacement = svLocals.get("&SYSLIST0", SymbolicVar("")).value
            continue
          replacement = indexValue(replacement, iv, depth)
      else:
        n = evalArithmeticExpression(exps[0], svLocals, stmt)
        if n == None:
          error(stmt, f"Cannot evaluate index of {sv}: {exps[0]!s}")
          continue
        n -= 1  # 1-based subscript -> 0-based
        # A non-positive subscript (k<=0) is a real error.
        if n < 0:
          error(stmt, f"Index of {sv}({n + 1}) out of range")
          continue
        if isinstance(replacement, (list, tuple)):
          # IBM: a subscript PAST THE END of a list-valued symbol (an
          # operand &SYSLIST, a sublist parameter &P1(k), or a
          # dimensioned SET array) is a NULL string, not an error.  Macros
          # walk operands/sublists with `AIF ('&P(k)' NE '').LOOP` and
          # rely on the reference going null to terminate.
          replacement = replacement[n] if n < len(replacement) else ""
        else:
          # A scalar (non-sublist) parameter behaves like a ONE-element
          # sublist: &P(1) is the value, &P(k>1) is null.  STKINS detects
          # "operand is not a parenthesized sublist" with
          # `AIF ('&P1(2)' EQ '').NOTSUBL`.
          replacement = replacement if n == 0 else ""
    if end < len(text) and text[end] == ".": # Optional "join" character.
      end += 1
    # bool must precede int (Python bool subclasses int): a SETB is
    # substituted as its bit value "0"/"1", not "False"/"True".
    if replacement is False:
      replacement = "0"
    elif replacement is True:
      replacement = "1"
    elif isinstance(replacement, int):
      replacement = str(replacement)
    elif isinstance(replacement, (list,tuple)):
      if text[start-2:start] in ["K'", "N'"]:
        start -= 2
        replacement = str(len(replacement))
      else:
        # A sublist parameter substituted WITHOUT a subscript renders as
        # the assembler sublist syntax "(e1,e2,...,eN)" -- WITH parens and
        # NO quotes -- so it re-parses as a sublist when passed to another
        # macro (IF -> IFPROC -> STKINS).  str(tuple) would inject quotes
        # and spaces that collapse to an empty operand on re-parse.
        replacement = "(" + ",".join(str(e) for e in replacement) + ")"
    text = text[:start] + replacement + text[end:]

  return text

# ===========================================================================
# Lark AST evaluators
# ===========================================================================

@dataclass
class EvalCtx:
  svLocals: object
  stmt: object = field(default_factory=Statement)
  symtab: dict = field(default_factory=dict)
  star: object = None
  severity: int = 255
  
  def error(self, msg):
    error(self.stmt, msg, self.severity)

  def svReplace(self, text):
    return svReplace(self.stmt, text, self.svLocals)

  def applySubscripts(self, value, idxs, name):
    return _applySubscripts(value, idxs, name, self)

  def sublistCount(self, value):
    return sublistCount(value)

  def isDefined(self, name):
    return name in definedNormalSymbols

  def lengthAttr(self, name):
    """The L' length attribute of program symbol `name`, or None (+ error)."""
    attrs = asmContext.symbolAttributes
    if isinstance(name, str) and name in attrs and "length" in attrs[name]:
      return attrs[name]["length"]
    self.error(f"Length attribute not available: L'{name!s}")
    return None

  def typeAttr(self, name):
    attrs = asmContext.symbolAttributes
    if isinstance(name, str) and name in attrs and "type" in attrs[name]:
      return attrs[name]["type"]
    return None

  def resolveLabel(self, name):
    symtab = self.symtab
    if name in symtab:
      entry = symtab[name]
      if asmContext.passCount == 3:
        if entry.references is None:
          entry.references = []
        entry.references.append(self.stmt.n)
      if entry.value is not None:
        value = entry.value
        try:
          if asmContext.passCount > 1 and isinstance(value, Reloc) \
                  and value.terms and not entry.dsect:
            value = symtab[asmContext.firstCSECT].value + \
                    symtab[entry.section].preliminaryOffset + \
                    entry.address
        except (KeyError, TypeError):
          # firstCSECT/section not laid out yet (early pass) -- keep the
          # un-reprojected value; a later pass redoes this.
          pass
        return value
    self.error("Eval error type 1")
    return None


def _applySubscripts(value, idxs, name, ctx):
  for depth, ixExpr in enumerate(idxs):
    iv = ixExpr.evalArith(ctx)
    if iv is None:
      ctx.error(f"Cannot evaluate index of {name}")
      return None, False
    if name == "&SYSLIST" and depth == 0 and iv == 0:
      value = ctx.svLocals.get("&SYSLIST0", SymbolicVar("")).value
      continue
    value = indexValue(value, iv, depth)
  return value, True

# Evaluate an arithmetic expression to an integer and return it, or else `None`
# on failure.  `stmt` is for the line of source code.  `expression` is
# the parsed expression, as returned by the parser function.  `svLocals` and
# `svGlobals` provide the defined symbolic variables, with the locals overriding
# the globals in case of overlap
def evalArithmeticExpression(expression, \
                             svLocals, \
                             stmt = None, \
                             symtab = None, \
                             star = None, \
                             severity = 255
                             ):
  if expression is None:        # a parse failure / absent operand
    return None
  return expression.evalArith(EvalCtx(svLocals, stmt or Statement(),
                                      symtab if symtab is not None else {},
                                      star, severity))

# Evaluate a boolean expression, returning True, False, or None (error).
def evalBooleanExpression(expression, svLocals, stmt = None):
  if expression is None:
    return None
  return expression.evalBool(EvalCtx(svLocals, stmt or Statement()))

# Evaluate a character expression to string and return it, or else `None`
# on failure.  `stmt` is for the line of source code.  `expression` is
# the parsed expression, as returned by the parser function.  `svLocals` and
# `svGlobals` provide the defined symbolic variables, with the locals overriding
# the globals in case of overlap
def evalCharacterExpression(expression, svLocals, stmt = None):
  if expression is None:
    return None
  return expression.evalChar(EvalCtx(svLocals, stmt or Statement()))

# Check whether two quantities are of the same type.  Only supported:
# int, boolean, string, list of int, list of boolean, list of string.
# Returns True if different types, False if same type.
def isDifferentType(q0, q1):
  if isinstance(q0, list) and isinstance(q1, list):
    # Two arrays are the same TYPE when their elements are; a differing
    # dimension (GBLC &T(264) vs GBLC &T(250)) is a compatible re-declaration
    # of one shared global array, not a type conflict (svDeclare grows it).
    e0 = q0[0] if q0 else ""
    e1 = q1[0] if q1 else ""
    return type(e0) != type(e1)
  return type(q0) != type(q1)

# Declaration of symbolic variables.  `operation` is one of "GBLA"..."LCLC".
# `operand` is a comma-separated list of so-far-undeclared symbolic variables
# (&A,&B,...) or subscripted ones (&A(&B) or &A(3)), where the dimension (&B or
# 3) is computable at assembly time.
def svDeclare(operation, operand, svLocals, stmt = None):
  global svGlobals
  stmt = stmt or Statement()
    
  # A declaration list continued across cards joins as "...&A,  &B..." -- the
  # break is always after a comma, padded by the first card's trailing blanks.
  # Collapse the blank(s) after a comma so the list re-forms, then take the
  # first blank-delimited token (dropping any trailing comment) and split it.
  fields = re.sub(r",[ \t]+", ",", operand).split()[0].split(",")
  typ = operation[3]
  if typ == "A":
    value = 0
  elif typ == "B":
    value = False
  else:
    value = ""
  originalValue = value
  isGlobal = operation.startswith("GBL")
  if isGlobal:
    sv = svGlobals
  else:
    sv = svLocals
  for fname in fields:
    value = originalValue
    if fname[:1] != "&":
      error(stmt, f"In {operation}, {fname} is not a symbolic variable")
      continue
    if "(" in fname:
      subfields = fname.split("(")
      if len(subfields) != 2 or subfields[1][-1:] != ")":
        error(stmt, f"In {operation}, {fname} is improperly formed")
        continue
      length = subfields[1][:-1]
      ast = parse(length, "arith_only")
      if ast == None:
        error(stmt, f"Could not parse dimension of {fname}")
        continue
      n = evalArithmeticExpression(ast, svLocals, stmt)
      if n == None:
        error(stmt, f"Could not compute dimension of {fname}")
        continue
      if n < 1:
        error(stmt, f"Dimension of {fname} out of range ({n})")
        continue
      fname = subfields[0]
      value = [value] * n
    if isGlobal:
      # Record that THIS scope declared `fname` as a global, so a later SET
      # may write through to it (see SymbolTable.lookupForSet).  A macro may
      # access a global SET symbol only if it declares it GBLx; otherwise the
      # process-wide `svGlobals` leaks an unrelated macro's global of the same
      # name into a macro using it as an undeclared local.
      svLocals.declareGlobal(fname)
    if fname in sv:
      existing = sv[fname].value
      if isDifferentType(existing, value):
        error(stmt, f"Attempt to change type of existing symbolic variable {fname}")
      elif isinstance(existing, list) and isinstance(value, list) \
              and len(value) > len(existing):
        # A compatible re-declaration with a larger dimension grows the
        # shared array so later indices (up to the larger bound) stay
        # in range regardless of which macro was loaded first.
        existing.extend([originalValue] * (len(value) - len(existing)))
      continue
    sv[fname] = SymbolicVar(value)

# Set a symbolic variable.  `operation` is one of "SETA", "SETB", "SETC".
# `name` and `operand` are strings.
def svSet(operation, name, operand, svLocals, stmt = None):
  global svGlobals
  stmt = stmt or Statement()
    
  operand = operand.strip()
  pname = parse(name, "nameset")
  if pname == None:
    error(stmt, f"Cannot parse name field {name}")
    return
  if "sv" not in pname:
    error(stmt, "No symbolic variable for assignment")
    return
  sname = pname["sv"]              # normally a bare string
  if isinstance(sname, list):
    sname = sname[0]
  # Resolve the write target: this scope if local, the global scope if `sname`
  # was declared GBLx here (the gate that stops a leaked process-wide global
  # from binding into an unrelated macro's undeclared local), else None.
  sv = svLocals.lookupForSet(sname)
  if sv is None:
    # Undeclared SET target.  System/360 requires prior declaration, but
    # AP-101 appears to auto-declare it as local -- including the SUBSCRIPTED
    # form: FCMBMTMC builds its comfault-mask tables with no LCLC at all
    # (`&APLHRM(1) SETC 'E0000000'` ...), so `&X(k) SETx` implicitly declares
    # a local array (grown on write below, HLASM-style).
    if operation == "SETA":
      dv = 0
    elif operation == "SETB":
      dv = False
    elif operation == "SETC":
      dv = ""
    else:
      error(stmt, "Instruction is not SETA, SETB, or SETC")
      return
    sv = svLocals
    svLocals[sname] = SymbolicVar([dv] if "exp" in pname else dv)
  v = sv[sname].value
  if isinstance(v, list):
    if "exp" not in pname:
      error(stmt, f"Is subscripted: {sname}")
      return
    # A representative value for datatype testing, NOT the indexed element.
    v = v[0]
  elif "exp" in pname:
    error(stmt, f"Is not subscripted: {sname}")
    return
  # A SET operand may be followed by a comment ("&A SETA &A+1  INCREMENT").
  # Parse with the non-anchored expression rules (not the "...Only" rules, which
  # require the whole field to be the expression via a trailing `$`); these stop
  # at the comment boundary.  The "...Only" rules would fail any SET-with-comment,
  # turning AIF/AGO counting loops into infinite loops.
  def evalOne(text):
    """Parse+evaluate one SET sub-operand per the operation's data type.
    Returns (ok, value); on a parse/type error emits it and returns ok=False."""
    if operation == "SETA" and isinstance(v, int):
      ast = parse(text, "arith")
      if ast == None:
        error(stmt, f"Cannot parse arithmetic expression {text}")
        return False, None
      return True, evalArithmeticExpression(ast, svLocals, stmt)
    elif operation == "SETB" and isinstance(v, bool):
      ast = parse(text, "bool")
      if ast == None:
        error(stmt, f"Cannot parse boolean expression {text}")
        return False, None
      return True, evalBooleanExpression(ast, svLocals, stmt)
    elif operation == "SETC" and isinstance(v, str):
      ast = parse(text, "cexpr")
      if ast == None:
        error(stmt, f"Cannot parse character expression {text}")
        return False, None
      value = evalCharacterExpression(ast, svLocals, stmt)
      if value != None:
        # Max SETC length is 255 (Assembler H / HLASM).  The OS/360
        # Assembler-F limit of 8 is too small -- e.g. FAZ2MAC's TITL macro
        # space-pads a SETC symbol to ~40 chars, and an 8-char cap turns its
        # padding loop into an infinite conditional-assembly loop.
        value = value[:255]
      return True, value
    else:
      error(stmt, f"Data type doesn't match {sname}")
      return False, None

  if "exp" not in pname:
    # Scalar target: a single value (the non-anchored parse ignores any
    # trailing comment).
    ok, value = evalOne(operand)
    if not ok:
      return
    if value == None:
      error(stmt, f"Unable to evaluate data expression {operand}")
      return
    sv[sname] = SymbolicVar(value)
    return

  # Subscripted target.  IBM allows a multi-value SET: comma-separated
  # sub-operands assigned to CONSECUTIVE array elements starting at the named
  # subscript (e.g. `&T9(1) SETC 'A','B',...,'Z'` fills &T9(1..26)).
  index = evalArithmeticExpression(pname["exp"], svLocals, stmt)
  if index == None:
    error(stmt, f"Cannot evaluate subscript in {name}")
    return
  index -= 1 # Change from 1-based to 0-based.
  pieces = split_top_level(operand)
  for offset, piece in enumerate(pieces):
    # For a single value parse the original operand (preserves the exact
    # non-anchored, comment-tolerant behavior); for a true list each piece
    # is already comma- and comment-free.
    ok, value = evalOne(operand if len(pieces) == 1 else piece)
    if not ok:
      return
    if value == None:
      error(stmt, f"Unable to evaluate data expression {operand}")
      return
    idx = index + offset
    if idx < 0:
      error(stmt, f"Index out of range: {name}")
      return
    arr = sv[sname].value
    if idx >= len(arr):
      # Writing past the current dimension GROWS the array (HLASM-style;
      # the AP-101 assembler must allow it -- FCMBMTMC assigns
      # `&APLHRM(1)`..`(16)` with no declared dimension at all).  Reads
      # past the end are already null (see svReplace).
      if isinstance(v, bool):
        filler = False
      elif isinstance(v, int):
        filler = 0
      else:
        filler = ""
      arr.extend([filler] * (idx + 1 - len(arr)))
    arr[idx] = value

