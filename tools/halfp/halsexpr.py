"""
Tiny HAL/S expression evaluator used by the literalSCALARS test.

Handles a deliberately small subset of HAL/S syntax:

  * decimal literals: 1.0, .5, -.0074239969, 1.0E-3
  * bare variable references: EARTH_MU
  * grouping: ( ... )
  * unary +/-, binary +/-, *, /, ** (exponentiation)
  * implicit multiplication: two adjacent atoms with whitespace between
    them mean multiplication ('A B' -> 'A * B').

HAL/S precedence as documented in the Programmer's Reference puts '+'
at level 6 and '-' at level 7 (lower), so 'a - b + c' would parse as
'a - (b + c)', and unary '-' would consume the entire sub-expression
to its right.  HOWEVER, the binaries produced by HAL/S-FC tell a
different story: in literalSCALARS-exp2.txt, expressions like
'(0.875-1.5+0.75)/(1.0-1.5+0.625)-0.875' compile to a value that only
matches under C-style precedence — '+' and '-' at the same level,
left-to-right, with tight unary minus.  See
tools/halfp/precedence_survey.py for the evidence (15/15 formula-only
SIGNIFICANT diffs match under c_lr, 0/15 under the documented spec).

So the parser implements the empirical HAL/S-FC precedence:

    1.  **            exponentiation        right-to-left
    2.  *             multiplication        left-to-right
    3.  *             cross-product         (vector-only)
    4.  .             dot product           (vector-only)
    5.  /             division              right-to-left
    6.  +, -          additive (same level) left-to-right
        +(unary)      identity
        -(unary)      tight: binds only to next factor (the next
                      mul-level operand), not the whole add-level chain.

The departure from C is that '/' is right-associative and lower
precedence than '*'.  We don't have a chained-'/' literal in the
dataset to confirm right-assoc empirically, but it's consistent with
the spec and no observed binaries contradict it.

Variables are looked up in a name -> 16-char-hex DP value map.
Arithmetic is delegated to the C halfp_driver so the result is bit-exact
to MONITOR(9)'s ibm_dp_* primitives.

Failures (parse errors, unknown variables, divide-by-zero) raise
EvalError; callers catch and route to an "unresolved" report bucket.
"""

import re
from typing import Optional


class EvalError(Exception):
    pass


# ---- Tokenizer -----------------------------------------------------

_NUM_RE = re.compile(r'\d+\.?\d*(?:[Ee][+-]?\d+)?|\.\d+(?:[Ee][+-]?\d+)?')
_ID_RE  = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def tokenize(s: str):
    """Tokenize a HAL/S expression string. Returns list of (kind, value).
    Kinds: 'NUM', 'ID', 'OP'. Whitespace is dropped after tokenization,
    but adjacent atoms with no operator between get a virtual ('OP', '*').
    """
    toks = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        # ** must be matched before single *
        if c == '*' and i + 1 < n and s[i+1] == '*':
            toks.append(('OP', '**')); i += 2; continue
        if c in '+-*/()':
            toks.append(('OP', c)); i += 1; continue
        m = _NUM_RE.match(s, i)
        if m and m.group():
            toks.append(('NUM', m.group())); i = m.end(); continue
        m = _ID_RE.match(s, i)
        if m and m.group():
            toks.append(('ID', m.group())); i = m.end(); continue
        raise EvalError(f"unexpected character {c!r} at position {i} in {s!r}")
    # Insert virtual '*' between adjacent atoms (the HAL/S "AB" = "A*B" rule).
    out = []
    for j, t in enumerate(toks):
        if j > 0:
            prev = toks[j-1]
            prev_atom = prev[0] in ('NUM', 'ID') or prev == ('OP', ')')
            cur_atom  = t[0]    in ('NUM', 'ID') or t    == ('OP', '(')
            if prev_atom and cur_atom:
                out.append(('OP', '*'))
        out.append(t)
    return out


# ---- Parser --------------------------------------------------------

class _Parser:
    def __init__(self, tokens):
        self.toks = tokens
        self.pos = 0

    def _peek(self):
        return self.toks[self.pos] if self.pos < len(self.toks) else (None, None)

    def _eat(self, expected=None):
        if self.pos >= len(self.toks):
            raise EvalError("unexpected end of expression")
        t = self.toks[self.pos]
        if expected and t != expected:
            raise EvalError(f"expected {expected}, got {t}")
        self.pos += 1
        return t

    def parse(self):
        ast = self._sub()
        if self.pos != len(self.toks):
            raise EvalError(f"trailing tokens: {self.toks[self.pos:]}")
        return ast

    # Level 6: additive (+ and -) at the same level, left-to-right.
    # Unary - is tight: it binds only to the next factor (one _div),
    # not the whole add-level chain.  Empirical match to HAL/S-FC's
    # compiled binaries — see module docstring.  The compile-time
    # folding path that this models is PASS1.PROCS/ADDANDSU.xpl:119-126
    # (ARITH_LITERAL pulls both operands from the literal table into
    # DW(0..3), inline DP add/sub, SAVE_LITERAL the result), iterated
    # left-to-right per the standard recursive-descent parse.
    def _sub(self):
        if self._peek() == ('OP', '-'):
            self._eat()
            left = ('neg', self._div())
        elif self._peek() == ('OP', '+'):
            self._eat()
            left = self._div()  # unary + is identity
        else:
            left = self._div()
        while self._peek() in (('OP', '-'), ('OP', '+')):
            op = self._eat()[1]
            right = self._div()
            left = (op, left, right)
        return left

    # Level 5: division, right-to-left.
    def _div(self):
        left = self._mul()
        if self._peek() == ('OP', '/'):
            self._eat()
            right = self._div()  # right-associative
            return ('/', left, right)
        return left

    # Level 2: multiplication, left-to-right.  (Levels 3 and 4 — cross
    # and dot product — only apply to vectors and never appear in scalar
    # initials, so they collapse into this level for our subset.)
    def _mul(self):
        left = self._pow()
        while self._peek() == ('OP', '*'):
            self._eat()
            right = self._pow()
            left = ('*', left, right)
        return left

    # Level 1 (highest): exponentiation, right-to-left.
    def _pow(self):
        base = self._atom()
        if self._peek() == ('OP', '**'):
            self._eat()
            # Right-assoc; recurse back into _pow so the next ** chains.
            # Note: per the precedence table a unary - on the right of **
            # is at level 7, which is *lower* than ** — so '-A**2' parses
            # as the unary parser binding A**2 first, then negating.
            exp = self._pow()
            return ('**', base, exp)
        return base

    def _atom(self):
        t = self._peek()
        if t == ('OP', '('):
            self._eat()
            e = self._sub()
            self._eat(('OP', ')'))
            return e
        if t[0] == 'NUM':
            return ('num', self._eat()[1])
        if t[0] == 'ID':
            return ('var', self._eat()[1])
        raise EvalError(f"expected atom, got {t}")


def parse(s: str):
    return _Parser(tokenize(s)).parse()


# ---- Evaluator -----------------------------------------------------

class Evaluator:
    """Walk the AST and compute a 16-char DP hex string. Arithmetic
    is delegated to the supplied driver (HalfpDriver) so it follows
    the same code path as the runtime."""

    def __init__(self, driver, var_map):
        self.drv = driver
        self.vars = var_map  # name -> 16-char hex string

    def eval(self, ast) -> str:
        kind = ast[0]
        if kind == 'num':
            return self.drv.dp_from_string(ast[1])
        if kind == 'var':
            name = ast[1]
            if name not in self.vars:
                raise EvalError(f"undefined variable {name!r}")
            return self.vars[name]
        if kind == 'neg':
            inner = self.eval(ast[1])
            return self.drv.dp_neg(inner)
        if kind in ('+', '-', '*', '/', '**'):
            a = self.eval(ast[1])
            b = self.eval(ast[2])
            r = self.drv.dp_arith(kind, a, b)
            if r.startswith("ERR"):
                raise EvalError(r)
            return r
        raise EvalError(f"unknown AST node {ast}")


def evaluate(driver, var_map, expression: str) -> str:
    """Top-level: parse + evaluate. Returns 16-char DP hex on success.
    Raises EvalError on any failure (unknown variable, parse error,
    divide-by-zero, etc.)."""
    ast = parse(expression)
    return Evaluator(driver, var_map).eval(ast)


def collect_vars(expression: str) -> set:
    """Return the set of identifier names referenced by an expression.
    Used by the unresolved-dump path to record every variable the entry
    touched, not just the first one EvalError flagged as missing."""
    seen: set = set()
    def walk(node):
        if node[0] == 'var':
            seen.add(node[1])
            return
        if node[0] in ('num',):
            return
        if node[0] == 'neg':
            walk(node[1]); return
        if node[0] in ('+', '-', '*', '/', '**'):
            walk(node[1]); walk(node[2]); return
    walk(parse(expression))
    return seen
