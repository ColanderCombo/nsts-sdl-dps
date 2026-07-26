"""
Reverse-engineer the math constants (DEG_RAD, DEGHR_RADSEC, PI, ...)
from the SP-binary targets of expressions that reference them.

Given a reference like '260 CGMK_DEG_RAD' with SP binary target
T = 0x41489B0F, the COMPOOL's DEG_RAD must be a DP value such that
ibm_dp_mul(260, DEG_RAD) truncates to T.  Solve DEG_RAD = T / 260
through the IBM-DP driver to get one candidate value, then verify it
against every other reference using the same constant.

Approach:
  1. Group sub-threshold-diff entries by which constant they reference.
  2. For each constant, take each reference and solve for the constant
     value via the inverse operation (driver-arithmetic, IBM-DP exact).
  3. Pick the candidate that satisfies the most references and report.

Run from repo root:
    PYTHONPATH=. python3 tools/halfp/derive_constants.py
"""

import json
import sys
from pathlib import Path
from collections import Counter, defaultdict

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tools.halfp.parse_literal_scalars import parse_file
from tools.halfp.halsexpr import (
    evaluate as eval_halexpr, EvalError, tokenize, collect_vars,
)
from tools.halfp.halfp_runner import HalfpDriver

DATASET = REPO_ROOT / "testCases/vagc#1296/literalSCALARS-exp2.txt"
DRIVER  = REPO_ROOT / "build/test/halfp_driver"
SIDECAR = REPO_ROOT / "tools/halfp/variable_values.json"

CONSTANT_GROUPS = [
    ("CGMK_DEG_RAD",      ["CGMK_DEG_RAD", "CGCK_DEG_RAD", "DEG_RAD"]),
    ("CGMK_DEGHR_RADSEC", ["CGMK_DEGHR_RADSEC", "CGCK_DEGHR_RADSEC", "DEGHR_RADSEC"]),
    ("CGIK_PI",           ["CGIK_PI", "CGCK_PI", "CGMK_PI", "CSMK_PI", "PI"]),
]


class _Drv:
    def __init__(self, drv): self.drv = drv
    def dp_from_string(self, s): return self.drv.dp_from_string(s)
    def dp_neg(self, h):         return self.drv.dp_neg(h)
    def dp_arith(self, op, a, b):return self.drv.dp_arith(op, a, b)


def hex_eq(actual_dp, target, type_class):
    if type_class == "SP":
        return actual_dp[:8].lower() == target.lower()
    return actual_dp.lower() == target.lower()


def collect_refs(values, var_map, aliases):
    out = []
    for v in values:
        if v.source_literal is None or v.asterisked:
            continue
        try:
            vars_in = collect_vars(v.source_literal)
        except EvalError:
            continue
        if not (set(aliases) & vars_in):
            continue
        missing = [n for n in vars_in
                   if n not in var_map and n not in aliases]
        if missing:
            continue
        out.append(v)
    return out


def main():
    values = parse_file(str(DATASET))
    sidecar = json.loads(SIDECAR.read_text())
    base_var_map = {k: v for k, v in sidecar.items() if not k.startswith("_")}

    var_map = dict(base_var_map)
    with HalfpDriver(str(DRIVER)) as drv:
        wrap = _Drv(drv)
        # Pre-walk to register file-defined values & aliases.
        for v in values:
            dp_hex = (v.hex if v.type_class == "DP"
                      else v.hex + "00000000")
            if (v.aggregate_size == 1 and v.aggregate_kind is None
                    and v.storage_class == "SCALAR"):
                var_map.setdefault(v.name, dp_hex)
            if v.source_literal:
                try:
                    toks = tokenize(v.source_literal)
                    if len(toks) == 1 and toks[0][0] == 'ID':
                        var_map.setdefault(toks[0][1], dp_hex)
                except EvalError:
                    pass

        for canonical, aliases in CONSTANT_GROUPS:
            print()
            print(f"=== {canonical} ===")
            current = base_var_map[canonical]
            print(f"  current sidecar: {current}")

            refs = collect_refs(values, var_map, aliases)
            print(f"  references found: {len(refs)}")

            # Baseline: how many refs match with current value?
            base_match = 0
            for v in refs:
                try:
                    got = eval_halexpr(wrap, var_map, v.source_literal)
                    if hex_eq(got, v.hex, v.type_class):
                        base_match += 1
                except EvalError:
                    pass
            print(f"  current matches: {base_match}/{len(refs)}")

            # For each ref of the form '<num> ALIAS' (a clean scaled
            # multiplication), solve for the constant directly.
            candidates = Counter()  # candidate_hex -> votes
            candidate_origin = {}   # hex -> example reference loc

            for v in refs:
                # Need to know which alias appears so we can isolate.
                vars_in = collect_vars(v.source_literal)
                alias_used = next(iter(set(aliases) & vars_in), None)
                if alias_used is None:
                    continue

                # Algebraic inversion only works cleanly for the
                # canonical "<integer or simple decimal> ALIAS" shape
                # (and a couple of friendlier patterns).  We try:
                #   "<num> ALIAS"        -> ALIAS = target / num
                #   "<num> ALIAS .<num2>"-> ALIAS = target / (num * num2)
                #   "<num> / ALIAS"      -> ALIAS = num / target
                lit = v.source_literal.strip()
                try:
                    toks = tokenize(lit)
                except EvalError:
                    continue

                # Promote SP target to DP form for division.
                target_dp = (v.hex if v.type_class == "DP"
                             else v.hex + "00000000")
                target_dp = target_dp.lower()

                # Strip the virtual '*' tokens inserted by tokenize() so
                # patterns can be expressed in user-visible form.
                ut = [t for t in toks if t != ('OP', '*')]

                cand = None
                ALIAS = ('ID', alias_used)

                # Pattern A: NUM ALIAS  -> ALIAS = target / NUM
                if (len(ut) == 2 and ut[0][0] == 'NUM' and ut[1] == ALIAS):
                    n = drv.dp_from_string(ut[0][1])
                    cand = drv.dp_arith('/', target_dp, n)

                # Pattern A2: ALIAS NUM  -> ALIAS = target / NUM
                elif (len(ut) == 2 and ut[0] == ALIAS and ut[1][0] == 'NUM'):
                    n = drv.dp_from_string(ut[1][1])
                    cand = drv.dp_arith('/', target_dp, n)

                # Pattern B: NUM ALIAS NUM2  -> ALIAS = target / (NUM*NUM2)
                elif (len(ut) == 3 and ut[0][0] == 'NUM' and ut[1] == ALIAS
                        and ut[2][0] == 'NUM'):
                    n1 = drv.dp_from_string(ut[0][1])
                    n2 = drv.dp_from_string(ut[2][1])
                    prod = drv.dp_arith('*', n1, n2)
                    cand = drv.dp_arith('/', target_dp, prod)

                # Pattern C: NUM / ALIAS  -> ALIAS = NUM / target
                elif (len(ut) == 3 and ut[0][0] == 'NUM'
                        and ut[1] == ('OP', '/') and ut[2] == ALIAS):
                    n = drv.dp_from_string(ut[0][1])
                    cand = drv.dp_arith('/', n, target_dp)

                # Pattern D: NUM NUM2 / ALIAS  -> ALIAS = (NUM*NUM2)/target
                elif (len(ut) == 4 and ut[0][0] == 'NUM' and ut[1][0] == 'NUM'
                        and ut[2] == ('OP', '/') and ut[3] == ALIAS):
                    n1 = drv.dp_from_string(ut[0][1])
                    n2 = drv.dp_from_string(ut[1][1])
                    prod = drv.dp_arith('*', n1, n2)
                    cand = drv.dp_arith('/', prod, target_dp)

                # Pattern E: NUM ALIAS / NUM2  -> ALIAS = target * NUM2 / NUM
                elif (len(ut) == 4 and ut[0][0] == 'NUM' and ut[1] == ALIAS
                        and ut[2] == ('OP', '/') and ut[3][0] == 'NUM'):
                    n1 = drv.dp_from_string(ut[0][1])
                    n2 = drv.dp_from_string(ut[3][1])
                    cand = drv.dp_arith('*', target_dp, n2)
                    cand = drv.dp_arith('/', cand, n1)

                if cand is not None and not cand.upper().startswith("ERR"):
                    candidates[cand.lower()] += 1
                    candidate_origin.setdefault(
                        cand.lower(),
                        f"{v.file_section}:{v.line_number}:{v.name}"
                        f"  '{v.source_literal}'")

            print(f"  derived {len(candidates)} distinct candidates from "
                  f"{sum(candidates.values())} refs")
            top = candidates.most_common(8)
            for h, n in top:
                print(f"    {h}  votes={n}")

            # For each candidate, count how many refs it satisfies.
            best = (-1, None)
            print()
            print("  matches per candidate:")
            for cand_hex, _ in top:
                trial_map = dict(var_map)
                for a in aliases:
                    trial_map[a] = cand_hex
                m = 0
                for v in refs:
                    try:
                        got = eval_halexpr(wrap, trial_map, v.source_literal)
                        if hex_eq(got, v.hex, v.type_class):
                            m += 1
                    except EvalError:
                        pass
                tag = "  <-- best" if m > best[0] else ""
                if m > best[0]:
                    best = (m, cand_hex)
                # Show diff vs current for context.
                diff = int(cand_hex, 16) - int(current, 16)
                print(f"    {cand_hex}  matches={m}/{len(refs)}  "
                      f"Δ={diff:+d}{tag}")

            if best[0] > base_match:
                print(f"  BEST: {best[1]} matches {best[0]}/{len(refs)} "
                      f"(current: {base_match})")
                print(f"  origin: {candidate_origin.get(best[1], '?')}")
            else:
                print(f"  No candidate beats the current "
                      f"({base_match}/{len(refs)})")

            # Identify which refs fail under current value, and what
            # DEG_RAD each individually wants.
            print()
            print("  Failing refs under current sidecar:")
            for v in refs:
                try:
                    got = eval_halexpr(wrap, var_map, v.source_literal)
                    if hex_eq(got, v.hex, v.type_class):
                        continue
                except EvalError:
                    continue
                # Find which derived candidate (if any) fixes THIS one.
                fixers = []
                for cand_hex, _ in candidates.most_common():
                    trial_map = dict(var_map)
                    for a in aliases:
                        trial_map[a] = cand_hex
                    try:
                        got2 = eval_halexpr(wrap, trial_map, v.source_literal)
                        if hex_eq(got2, v.hex, v.type_class):
                            fixers.append(cand_hex)
                    except EvalError:
                        pass
                loc = (f"{v.file_section}:{v.line_number}:{v.name}"
                       + (f"[{v.aggregate_index}]"
                          if v.aggregate_kind else ""))
                ours_dp = got
                ours_sp = ours_dp[:8]
                print(f"    {loc:<55}")
                print(f"      target={v.hex.upper()}  ours={ours_sp.upper()}  "
                      f"DP-eval-LSW={ours_dp[8:].upper()}")
                print(f"      expr: {v.source_literal}")
                if fixers:
                    print(f"      would match under: {fixers[0]}")
                else:
                    print(f"      no derived candidate fixes this one")


if __name__ == "__main__":
    main()
