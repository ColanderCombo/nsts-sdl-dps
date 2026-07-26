"""
Generate candidate parenthesizations for the deviating expressions and
validate each by evaluating against the target hex.

Most of the deviations follow a small handful of recognizable patterns
common in HAL/S source — straight-line interpolation slopes/intercepts
and a few specific algebraic forms. We try each of those patterns
mechanically; whatever matches goes into the sidecar.

Anything that doesn't match a pattern is left for human (or subagent)
attention.

Usage:
    find_paren_fixups.py <deviations.json> <driver-binary> <out.json>
"""

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from tools.halfp.halsexpr import tokenize, EvalError, evaluate
from tools.halfp.halfp_runner import HalfpDriver


def split_top_minus(expr: str):
    """Split a flat expression on top-level binary '-'.  Returns the list
    of pieces (which is `[expr]` if there's no top-level minus)."""
    # We don't need a full parse here; the deviations are dense numeric
    # text with at most one top-level '-', or a chain of them.  Tokenize
    # and group.
    try:
        toks = tokenize(expr)
    except EvalError:
        return None
    pieces = [[]]
    depth = 0
    for t in toks:
        if t == ('OP', '('):
            depth += 1; pieces[-1].append(t); continue
        if t == ('OP', ')'):
            depth -= 1; pieces[-1].append(t); continue
        if depth == 0 and t == ('OP', '-'):
            pieces.append([])
            continue
        pieces[-1].append(t)
    return pieces


def join_toks(toks) -> str:
    out = []
    for t in toks:
        if t[0] == 'OP':
            out.append(t[1])
        else:
            out.append(t[1])
    return ' '.join(out)


def candidate_rewrites(expr: str):
    """Yield a sequence of candidate parenthesized forms for `expr`.

    Each candidate is a string we'll evaluate; the first one that
    matches the target hex wins. We err on the side of being explicit
    rather than clever — readability of the sidecar matters."""

    e = expr.strip()

    # Pattern A: "a - b / c - d"  →  "(a - b) / (c - d)"
    # Detected by: 4 numeric (or identifier) tokens, separated by - / -.
    # We use a text-level regex tolerant of spaces.
    m = re.fullmatch(
        r'\s*([+\-]?[\w.]+(?:[Ee][+\-]?\d+)?)\s*-\s*'
        r'([+\-]?[\w.]+(?:[Ee][+\-]?\d+)?)\s*/\s*'
        r'([+\-]?[\w.]+(?:[Ee][+\-]?\d+)?)\s*-\s*'
        r'([+\-]?[\w.]+(?:[Ee][+\-]?\d+)?)\s*',
        e)
    if m:
        a, b, c, d = m.groups()
        yield f"({a} - {b}) / ({c} - {d})"

    # Pattern B: "y1 x1-x2 a-b/c-d"  →  "y1 - x1 * (a - b) / (c - d)"
    # i.e. straight-line intercept "y1 - x1*slope" where slope = (a-b)/(c-d)
    # Tokens: NUM, NUM, '-', NUM, NUM, '-', NUM, '/', NUM, '-', NUM
    # The "x1-x2" middle group is a separate piece often appearing
    # adjacent to the slope formula.
    # Simpler pattern: 5 numbers grouped as (n1 n2-n3 n4-n5/n6-n7) is rare;
    # the literal form encountered is "Y0 X0-X1 (a-b)/(c-d)" → Y0 - X0 * (a-b)/(c-d).
    m = re.fullmatch(
        r'\s*([+\-]?[\w.]+(?:[Ee][+\-]?\d+)?)\s*-\s*'  # y1 then '-'
        r'([+\-]?[\w.]+(?:[Ee][+\-]?\d+)?)\s+'         # x1
        r'([+\-]?[\w.]+(?:[Ee][+\-]?\d+)?)\s*-\s*'     # a
        r'([+\-]?[\w.]+(?:[Ee][+\-]?\d+)?)\s*/\s*'     # b
        r'([+\-]?[\w.]+(?:[Ee][+\-]?\d+)?)\s*-\s*'     # c
        r'([+\-]?[\w.]+(?:[Ee][+\-]?\d+)?)\s*',        # d
        e)
    if m:
        y1, x1, a, b, c, d = m.groups()
        yield f"{y1} - {x1} * ({a} - {b}) / ({c} - {d})"

    # Pattern C: "1 - X**2 - 1"  →  "(1 - X)**2 - 1"
    m = re.fullmatch(
        r'\s*1(?:\.0)?\s*-\s*([\w.]+)\s*\*\*\s*2\s*-\s*1(?:\.0)?\s*', e)
    if m:
        x = m.group(1)
        yield f"(1 - {x})**2 - 1"

    # Pattern D: "1.0 - 1.0 - 1.0 / N ** 2"  →  "1.0 - (1.0 - 1.0/N)**2"
    m = re.fullmatch(
        r'\s*1\.0?\s*-\s*1\.0?\s*-\s*1\.0?\s*/\s*([\w.]+)\s*\*\*\s*2\s*', e)
    if m:
        n = m.group(1)
        yield f"1.0 - (1.0 - 1.0/{n})**2"

    # Pattern E: "1 - A  B"  →  "(1 - A) * B"  (space-mult at end)
    m = re.fullmatch(
        r'\s*1(?:\.0)?\s*-\s*([\w.]+)\s+([\w.]+)\s*', e)
    if m:
        a, b = m.groups()
        yield f"(1 - {a}) * {b}"

    # Pattern F: "X .NUM / NUM ** N"  →  "X * (.NUM / NUM)**N"
    # E.g. EARTH_MU .3048/1852.**3  →  EARTH_MU * (.3048 / 1852.)**3
    m = re.fullmatch(
        r'\s*([\w]+)\s+(\.\d+)\s*/\s*(\d+\.?\d*)\s*\*\*\s*(\d+)\s*', e)
    if m:
        v, n1, n2, exp = m.groups()
        yield f"{v} * ({n1}/{n2})**{exp}"

    # Pattern G: "1 - X ** 2"  →  "(1 - X)**2"  (no trailing -1)
    m = re.fullmatch(
        r'\s*1(?:\.0)?\s*-\s*([\w.]+)\s*\*\*\s*2\s*', e)
    if m:
        x = m.group(1)
        yield f"(1 - {x})**2"

    # Pattern H: "1.0 - 1.0 / N ** 2"  →  "(1.0 - 1.0/N)**2"
    m = re.fullmatch(
        r'\s*1\.0?\s*-\s*1\.0?\s*/\s*([\w.]+)\s*\*\*\s*2\s*', e)
    if m:
        n = m.group(1)
        yield f"(1.0 - 1.0/{n})**2"

    # Pattern I (linear-fit y-intercept variant, identifiers only):
    # "A - B C/1 + C"  →  "A - B*C/(1 + C)"
    # E.g. "GDAMS2 - GDAMS1 GDAMS3/1 + GDAMS3"  →  "GDAMS2 - GDAMS1*GDAMS3/(1 + GDAMS3)"
    # Note +0 has higher precedence than - in HAL/S, so 1+C is already a unit.
    m = re.fullmatch(
        r'\s*([\w]+)\s*-\s*([\w]+)\s+([\w]+)\s*/\s*1\s*\+\s*([\w]+)\s*', e)
    if m:
        a, b, c, d = m.groups()
        yield f"{a} - {b}*{c}/(1 + {d})"


class _DrvWrap:
    def __init__(self, drv): self.drv = drv
    def dp_from_string(self, s): return self.drv.dp_from_string(s)
    def dp_neg(self, h): return self.drv.dp_neg(h)
    def dp_arith(self, op, a, b): return self.drv.dp_arith(op, a, b)


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        sys.exit(2)
    deviations_path, driver_path, out_path = argv

    deviations = json.loads(Path(deviations_path).read_text())
    fixups = {}
    unresolved = []

    with HalfpDriver(driver_path) as drv:
        wrap = _DrvWrap(drv)
        for d in deviations:
            expr = d['expression']
            target = d['target_hex']
            type_class = d['type_class']
            var_map = d.get('referenced_vars', {})
            # If any var is undefined, we can't validate — skip.
            if any(v == '<undefined>' for v in var_map.values()):
                unresolved.append({**d, 'reason': 'undefined variable'})
                continue
            tried = []
            matched = None
            for cand in candidate_rewrites(expr):
                tried.append(cand)
                try:
                    r = evaluate(wrap, var_map, cand)
                    if type_class == 'SP':
                        ours = r[:8]
                    else:
                        ours = r
                except EvalError as e:
                    continue
                if ours == target:
                    matched = cand
                    break
            if matched:
                # Last-rewrite-wins if duplicates; sanity-check below.
                if expr in fixups and fixups[expr] != matched:
                    print(f'WARN: conflicting rewrite for {expr!r}: '
                          f'{fixups[expr]!r} vs {matched!r}', file=sys.stderr)
                fixups[expr] = matched
            else:
                unresolved.append({**d, 'tried': tried})

    Path(out_path).write_text(json.dumps(fixups, indent=2, sort_keys=True))
    print(f'Generated fixups: {len(fixups)}')
    print(f'Unresolved:       {len(unresolved)}')
    print(f'Wrote {out_path}')

    print()
    print('Unresolved (first 30):')
    for u in unresolved[:30]:
        tried = u.get('tried', [])
        reason = u.get('reason', f'no pattern matched ({len(tried)} tries)')
        print(f'  {u["type_class"]} {u["target_hex"]} {u["expression"]!r}')
        print(f'      → {reason}')


if __name__ == "__main__":
    main(sys.argv[1:])
