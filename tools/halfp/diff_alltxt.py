"""
Compare the constants in our sidecar (tools/halfp/data/variable_values.json)
against the literal declarations recovered in
testCases/vagc#1296/all.txt, and propose updates.

For each name in the sidecar that also has a declaration in all.txt:
  - Parse all.txt's literal via dp_from_string (or evaluate if it's an
    expression like '1/CGIK_DEG_RAD').
  - Compare to current sidecar value.
  - Report the diff plus any caveats (asterisked-binary collisions,
    expressions that need other constants resolved first, ...).

Does NOT modify the sidecar.  Output is a proposal you review before
committing.

Run from repo root:
    PYTHONPATH=. python3 tools/halfp/diff_alltxt.py
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tools.halfp.halsexpr import (
    evaluate as eval_halexpr, EvalError, tokenize,
)
from tools.halfp.halfp_runner import HalfpDriver

ALLTXT_PATH = REPO_ROOT / "testCases/vagc#1296/all.txt"
SIDECAR_PATH = REPO_ROOT / "tools/halfp/data/variable_values.json"
DRIVER_PATH = REPO_ROOT / "build/test/halfp_driver"

# Match a clean line: NAME `value`  (single value, no embedded backticks
# in the value, no extra fields).  Multi-value or HEX'/BIN'-style lines
# are skipped because they're not what we care about for FP scalars.
LINE_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\s+`([^`]*)`\s*$')

# Numeric-literal pattern (decimal int or float, optional sign, optional
# scientific notation with 'E').  Skip lines whose value isn't purely
# numeric.
NUM_RE = re.compile(
    r'^[+\-]?(?:\d+\.?\d*|\.\d+)(?:[Ee][+\-]?\d+)?$'
)

# Expression heuristic: contains an identifier or arithmetic operator.
# We try eval_halexpr after tokenizing.
EXPR_OP_RE = re.compile(r'[+\-*/()A-Za-z_]')


class _D:
    def __init__(self, drv): self.drv = drv
    def dp_from_string(self, s): return self.drv.dp_from_string(s)
    def dp_neg(self, h):         return self.drv.dp_neg(h)
    def dp_arith(self, op, a, b):return self.drv.dp_arith(op, a, b)


def parse_all_txt(path):
    """Return dict: NAME -> raw value string, for clean single-value lines."""
    out = {}
    skipped = 0
    for ln in path.read_text().splitlines():
        m = LINE_RE.match(ln)
        if not m:
            skipped += 1
            continue
        out[m.group(1)] = m.group(2).strip()
    return out, skipped


def main():
    sidecar = json.loads(SIDECAR_PATH.read_text())
    sidecar_keys = {k for k in sidecar if not k.startswith("_")}

    all_decls, skipped = parse_all_txt(ALLTXT_PATH)
    print(f"all.txt: {len(all_decls)} clean single-value lines, "
          f"{skipped} skipped (multi-value, malformed, or non-string).")
    print()

    # Names in both: candidates for update.
    overlap = sidecar_keys & all_decls.keys()
    print(f"Sidecar entries also declared in all.txt: {len(overlap)}")
    print()

    # Categorize each overlap entry.
    matches    = []          # current sidecar already matches
    proposals  = []          # current sidecar should change to derived
    skipped_o  = []          # value not interpretable
    expressions = []         # value is an expression — needs other consts

    with HalfpDriver(str(DRIVER_PATH)) as drv:
        wrap = _D(drv)
        for name in sorted(overlap):
            cur = sidecar[name].lower()
            literal = all_decls[name]

            # Pure numeric literal: parse via dp_from_string.
            if NUM_RE.match(literal):
                try:
                    new_hex = drv.dp_from_string(literal).lower()
                except Exception as e:
                    skipped_o.append((name, literal, f"dp_from_string error: {e}"))
                    continue
                if new_hex == cur:
                    matches.append((name, literal, new_hex))
                else:
                    delta = int(new_hex, 16) - int(cur, 16)
                    proposals.append((name, literal, cur, new_hex, delta))
                continue

            # Expression — try evaluating against existing sidecar.
            if EXPR_OP_RE.search(literal):
                try:
                    new_hex = eval_halexpr(wrap, sidecar, literal).lower()
                    if new_hex == cur:
                        matches.append((name, f"={literal}", new_hex))
                    else:
                        delta = int(new_hex, 16) - int(cur, 16)
                        expressions.append((name, literal, cur, new_hex, delta))
                except EvalError as e:
                    expressions.append((name, literal, cur, None, f"err: {e}"))
                continue

            skipped_o.append((name, literal, "not numeric or expression"))

    # ---- Report ----
    print(f"=== Already match ({len(matches)}) ===")
    for name, lit, h in matches[:10]:
        print(f"  {name:<28}  {lit:<25}  -> {h}")
    if len(matches) > 10:
        print(f"  ... and {len(matches) - 10} more")
    print()

    print(f"=== Proposed updates: literal-derived ({len(proposals)}) ===")
    if proposals:
        print(f"  {'name':<28}  {'literal':<25}  {'current':<16}  {'proposed':<16}  Δ")
        for name, lit, old, new, delta in proposals:
            print(f"  {name:<28}  {lit:<25}  {old.upper()}  {new.upper()}  {delta:+d}")
    print()

    print(f"=== Expression-valued (need eval; {len(expressions)}) ===")
    for entry in expressions:
        if len(entry) == 5:
            name, lit, old, new, delta = entry
            if isinstance(delta, int):
                print(f"  {name:<28}  ={lit}")
                print(f"      current:  {old.upper()}")
                print(f"      proposed: {new.upper()}  (Δ={delta:+d})")
            else:
                print(f"  {name:<28}  ={lit}  -- {delta}")
    print()

    if skipped_o:
        print(f"=== Skipped ({len(skipped_o)}) ===")
        for name, lit, reason in skipped_o[:10]:
            print(f"  {name:<28}  {lit!r:<25}  {reason}")
        if len(skipped_o) > 10:
            print(f"  ... and {len(skipped_o) - 10} more")

    # Asterisked-binary collision check: if a proposed update will cause
    # the file's compiled binary (which we'd compare against) to differ
    # from our forward-derived value because the binary is asterisked,
    # flag it.  We don't have the dataset's asterisk info loaded here,
    # so just call out names that we know from prior analysis are
    # asterisked.
    print()
    print("=== Caveats ===")
    print("  CGCS_TDAXFDL22's binary is asterisk-patched to 5.0 but the")
    print("    declared literal is 10.0.  Updating TDAXFDL22 to the literal")
    print("    will cause the BANKERR_NYJET_* targets (also asterisked) to")
    print("    mismatch — that's correct behavior since those are real")
    print("    binary patches, not compile-time-derivable.")
    print("  CGCS_TGRAY6_1's binary is asterisk-patched to 2.5 but the")
    print("    declared literal is 10.0.  Same situation — TGRAY[3], [4]")
    print("    intercepts will move from 'asterisk-redundant exact' to")
    print("    'asterisk-significant differs'.")


if __name__ == "__main__":
    main()
