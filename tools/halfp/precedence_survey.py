"""
Survey all-numeric SIGNIFICANT-diff entries under alternative precedence
rules, reporting which (if any) become bit-exact matches.

The current halsexpr parser implements the HAL/S Programmer's Reference
table: + at level 6, - at level 7 (so 'a - b + c' = 'a - (b+c)') with
unary minus consuming everything to its right.  But the compiled
binaries for several literal-only chain expressions don't agree with
that — they look like C-style left-to-right + and -, with unary minus
binding tightly.

This script reads build/test/halfp/literal_scalars_v2_report.txt's SIGNIFICANT
section, filters to entries whose source literal contains no variables,
and re-evaluates each one under three alternative parsers:

    spec   HAL/S spec (matches current parser)
    c_lr   C-style: + and - same level, left-to-right; unary - tight
    c_loose Same as c_lr but unary - still consumes whole expression

For each entry the script reports which interpretation reproduces the
binary's hex.  If c_lr explains most of them, it's a strong signal that
HAL/S-FC actually used C-style precedence regardless of what the spec
says.
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tools.halfp.halsexpr import (
    tokenize, EvalError, Evaluator, _Parser as HalsParser
)
from tools.halfp.halfp_runner import HalfpDriver

DRIVER_PATH = REPO_ROOT / "build/test/halfp_driver"
REPORT_PATH = REPO_ROOT / "build/test/halfp/literal_scalars_v2_report.txt"


# ---- Alternative parsers (only differ in _sub / _add levels) ----

class CStyleParser(HalsParser):
    """C-style: + and - at the same level, left-to-right, with unary -
    binding tightly (only to the next factor at the * / level).  We
    override `_sub` directly so paren-internal recursion through
    `_atom -> _sub` picks up the new behavior too — overriding `parse`
    alone wouldn't reach the parens."""

    def _sub(self):
        if self._peek() == ('OP', '-'):
            self._eat()
            left = ('neg', self._div())
        elif self._peek() == ('OP', '+'):
            self._eat()
            left = self._div()
        else:
            left = self._div()
        while self._peek() in (('OP', '-'), ('OP', '+')):
            op = self._eat()[1]
            right = self._div()
            left = (op, left, right)
        return left


class CLooseParser(HalsParser):
    """C-style binary + and -, but with HAL/S's loose unary minus
    (consumes the whole add/sub expression to its right)."""

    def _sub(self):
        if self._peek() == ('OP', '-'):
            self._eat()
            return ('neg', self._sub())
        if self._peek() == ('OP', '+'):
            self._eat()
            return self._sub()
        left = self._div()
        while self._peek() in (('OP', '-'), ('OP', '+')):
            op = self._eat()[1]
            right = self._div()
            left = (op, left, right)
        return left


def evaluate_with(parser_cls, drv, var_map, expr):
    toks = tokenize(expr)
    ast = parser_cls(toks).parse()
    return Evaluator(drv, var_map).eval(ast)


# ---- Driver adapter ----

class _DrvAdapter:
    def __init__(self, drv): self.drv = drv
    def dp_from_string(self, s): return self.drv.dp_from_string(s)
    def dp_neg(self, h):         return self.drv.dp_neg(h)
    def dp_arith(self, op, a, b):return self.drv.dp_arith(op, a, b)


def hex_eq(actual_dp, target, type_class):
    if type_class == "SP":
        return actual_dp[:8] == target
    return actual_dp == target


# ---- Report parsing ----

# Each row in SIGNIFICANT diffs has the literal as the last column,
# preceded by ulps, ratio, ours_hex, binary_hex, ours_dec, mafgen_dec, location.
# Columns are space-separated but the literal can contain spaces, so we parse
# from the right by splitting on the last 'literal' marker.  The header line
# tells us where literal starts; simpler to use a regex.

ROW_RE = re.compile(
    r'^(?P<loc>\S+)\s+'
    r'(?P<mafgen>\S+)\s+'
    r'(?P<ours_dec>\S+)\s+'
    r'(?P<bin_hex>[0-9A-F]{4}(?:\s[0-9A-F]{4}){0,3})\s+'
    r'(?P<ours_hex>[0-9A-F]{4}(?:\s[0-9A-F]{4}){0,3})\s+'
    r'(?P<ratio>\S+(?:\s+\S+)?)\s+'
    r'(?P<ulps>\d+)\s+'
    r'(?P<literal>.+)$'
)

ID_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def parse_significant_section(report_text):
    """Return list of dicts with keys loc, type_class, target_hex, literal."""
    out = []
    in_section = False
    for line in report_text.splitlines():
        if line.startswith(" UNMARKED DEVIATIONS"):
            in_section = True
            continue
        if in_section and line.startswith("==="):
            # Section ended (next ===)
            if out:
                break
            continue
        if not in_section: continue
        if not line.strip(): continue
        if 'location' in line and 'literal' in line: continue
        m = ROW_RE.match(line)
        if not m: continue
        bh = m.group('bin_hex').replace(' ', '')
        type_class = "DP" if len(bh) == 16 else "SP"
        out.append({
            'loc':        m.group('loc'),
            'type_class': type_class,
            'target_hex': bh,
            'literal':    m.group('literal').strip(),
        })
    return out


def has_no_vars(literal):
    """True if the literal contains no identifier tokens."""
    try:
        toks = tokenize(literal)
    except EvalError:
        return False
    return all(t[0] != 'ID' for t in toks)


# ---- Main ----

def main():
    report = REPORT_PATH.read_text()
    rows = parse_significant_section(report)
    formula_rows = [r for r in rows if has_no_vars(r['literal'])]
    print(f"SIGNIFICANT diffs total:        {len(rows)}")
    print(f"  with no variable references:  {len(formula_rows)}")
    print()

    parsers = {
        'spec':    HalsParser,
        'c_lr':    CStyleParser,
        'c_loose': CLooseParser,
    }
    counts = {k: 0 for k in parsers}

    with HalfpDriver(str(DRIVER_PATH)) as drv:
        wrap = _DrvAdapter(drv)
        for r in formula_rows:
            results = {}
            for name, cls in parsers.items():
                try:
                    got = evaluate_with(cls, wrap, {}, r['literal'])
                    ok = hex_eq(got, r['target_hex'], r['type_class'])
                    results[name] = (ok, got[:8] if r['type_class']=='SP' else got)
                except EvalError as e:
                    results[name] = (False, f"err: {e}")
            for k, (ok, _) in results.items():
                if ok: counts[k] += 1

            tags = ' '.join(
                f"{name}={'YES' if ok else 'no '}"
                for name, (ok, _) in results.items())
            print(f"  {r['loc']}")
            print(f"    target={r['target_hex']:<20s}  {tags}")
            for name, (ok, val) in results.items():
                if not ok and not val.startswith('err'):
                    print(f"      {name}: got {val}")
            print(f"    {r['literal']}")
            print()

    print("=== Match counts (bit-exact to binary) ===")
    for name in parsers:
        print(f"  {name:<8}: {counts[name]:3d} / {len(formula_rows)}")


if __name__ == "__main__":
    main()
