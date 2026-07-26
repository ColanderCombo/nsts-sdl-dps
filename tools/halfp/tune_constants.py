"""
Search small DP-ULP perturbations of the math constants
(CGMK_DEG_RAD, CGMK_DEGHR_RADSEC, CGIK_PI, CGMK_RAD_DEG, ...) to see
whether a single, consistent DP value can make every referencing
expression hit its binary target bit-exact.

These constants are *not* defined anywhere in literalSCALARS-exp2.txt;
they live in a COMPOOL we don't have.  Today the sidecar holds them as
the DP-precise IBM hex of the standard mathematical values (π, π/180,
180/π, π/648000), but a 1- or 2-ULP shift in those packed values will
shift every product or quotient through them — so the actual COMPOOL
value may differ from ours in the LSBs.

The search:
  1. Collect every parsed expression that references a target constant.
  2. Walk the constant from current_hex - N to current_hex + N (DP-ULP).
  3. For each candidate, evaluate every expression and count bit-exact
     matches against the binary target_hex.
  4. Print the best candidate and the per-reference match status.

Usage (from repo root):
    PYTHONPATH=. python3 tools/halfp/tune_constants.py
"""

import json
import sys
from pathlib import Path
from collections import defaultdict

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

# Constants to tune.  Each row is (canonical_name, list of aliases that
# share the same DP value).  Tuning the canonical also tunes the aliases.
CONSTANT_GROUPS = [
    ("CGMK_DEG_RAD",      ["CGMK_DEG_RAD", "CGCK_DEG_RAD", "DEG_RAD"]),
    ("CGMK_RAD_DEG",      ["CGMK_RAD_DEG", "CGCK_RAD_DEG", "RAD_DEG"]),
    ("CGMK_DEGHR_RADSEC", ["CGMK_DEGHR_RADSEC", "CGCK_DEGHR_RADSEC", "DEGHR_RADSEC"]),
    ("CGIK_PI",           ["CGIK_PI", "CGCK_PI", "CGMK_PI", "CSMK_PI", "PI"]),
]

ULP_RANGE = 8     # search current_hex - 8 to current_hex + 8


class _Drv:
    def __init__(self, drv): self.drv = drv
    def dp_from_string(self, s): return self.drv.dp_from_string(s)
    def dp_neg(self, h):         return self.drv.dp_neg(h)
    def dp_arith(self, op, a, b):return self.drv.dp_arith(op, a, b)


def hex_eq(actual_dp_hex: str, target_hex: str, type_class: str) -> bool:
    if type_class == "SP":
        return actual_dp_hex[:8].lower() == target_hex.lower()
    return actual_dp_hex.lower() == target_hex.lower()


def perturb_hex(hex16: str, delta: int) -> str:
    """Add `delta` (signed) to the unsigned-magnitude integer view of
    a 16-char DP hex string.  Sign bit preserved.  Crude but adequate
    for tiny perturbations near the current value."""
    n = int(hex16, 16)
    sign = n & 0x8000000000000000
    mag  = n & 0x7FFFFFFFFFFFFFFF
    new_mag = max(0, mag + delta)
    return f"{(sign | new_mag):016X}"


def collect_referencing_entries(values, var_map, target_aliases):
    """Return list of (FPValue, expression, vars) for SP/DP entries
    whose expression references any of `target_aliases` directly OR
    transitively (via another alias)."""
    aliases = set(target_aliases)
    out = []
    for v in values:
        if v.source_literal is None:
            continue
        try:
            vars_in_expr = collect_vars(v.source_literal)
        except EvalError:
            continue
        if not (vars_in_expr & aliases):
            continue
        # All vars in the expression (besides the target aliases) must
        # be resolvable from var_map for the eval to succeed.
        missing = [n for n in vars_in_expr
                   if n not in var_map and n not in aliases]
        if missing:
            continue
        out.append((v, v.source_literal, vars_in_expr))
    return out


def main():
    values = parse_file(str(DATASET))
    sidecar = json.loads(SIDECAR.read_text())
    base_var_map = {k: v for k, v in sidecar.items() if not k.startswith("_")}

    # Walk the dataset once to register file-defined values & aliases —
    # mimics what test_literal_scalars_v2 does so var_map is realistic
    # at the moment each entry is evaluated.
    var_map = dict(base_var_map)
    with HalfpDriver(str(DRIVER)) as drv:
        wrap = _Drv(drv)
        for v in values:
            if v.source_literal is None:
                # No alias to register, but still mark the entry's name
                # as DP-promoted binary if it's a scalar.
                if (v.aggregate_size == 1 and v.aggregate_kind is None
                        and v.storage_class == "SCALAR"):
                    var_map.setdefault(v.name, v.hex if v.type_class == "DP"
                                        else v.hex + "00000000")
                continue
            # Try to evaluate so we can set the entry's own name as alias.
            try:
                full = eval_halexpr(wrap, var_map, v.source_literal)
            except EvalError:
                full = None
            # Register the entry's own name as alias (uses binary).
            dp_hex = (v.hex if v.type_class == "DP"
                      else v.hex + "00000000")
            if (v.aggregate_size == 1 and v.aggregate_kind is None
                    and v.storage_class == "SCALAR"):
                var_map.setdefault(v.name, dp_hex)
            # If source is bare identifier, also register that.
            try:
                toks = tokenize(v.source_literal)
            except EvalError:
                toks = None
            if toks and len(toks) == 1 and toks[0][0] == 'ID':
                var_map.setdefault(toks[0][1], dp_hex)

        # Per-constant search.
        for canonical, aliases in CONSTANT_GROUPS:
            current_hex = base_var_map.get(canonical)
            if current_hex is None:
                print(f"!! {canonical}: not in sidecar, skipping")
                continue
            entries = collect_referencing_entries(values, var_map, aliases)
            print()
            print(f"=== {canonical}  (current = {current_hex}) ===")
            print(f"    references: {len(entries)} SP/DP entries")

            best = (-1, None, None)   # (matches, delta, hex)
            results_by_delta = {}
            for delta in range(-ULP_RANGE, ULP_RANGE + 1):
                cand_hex = perturb_hex(current_hex, delta)
                # Override the constant under all aliases.
                trial_map = dict(var_map)
                for a in aliases:
                    trial_map[a] = cand_hex
                matched = 0
                per_entry = []
                for v, expr, _ in entries:
                    try:
                        got = eval_halexpr(wrap, trial_map, expr)
                    except EvalError:
                        per_entry.append(False)
                        continue
                    ok = hex_eq(got, v.hex, v.type_class)
                    if ok: matched += 1
                    per_entry.append(ok)
                results_by_delta[delta] = (matched, cand_hex, per_entry)
                if matched > best[0]:
                    best = (matched, delta, cand_hex)

            # Print the search profile.
            print(f"    matches per ULP delta:")
            for delta in range(-ULP_RANGE, ULP_RANGE + 1):
                m, h, _ = results_by_delta[delta]
                marker = "  <-- best" if (delta, h) == (best[1], best[2]) else ""
                print(f"      Δ={delta:+3d}  hex={h}  matches={m}/{len(entries)}{marker}")

            best_matches, best_delta, best_hex = best
            if best_delta == 0:
                print(f"    => current value already optimal "
                      f"({best_matches}/{len(entries)} match)")
            else:
                print(f"    => best Δ={best_delta:+d} would give "
                      f"{best_matches}/{len(entries)} matches "
                      f"(current gives {results_by_delta[0][0]})")
                # Show which entries change between current and best.
                cur_pe = results_by_delta[0][2]
                best_pe = results_by_delta[best_delta][2]
                print(f"    flips on switching to Δ={best_delta:+d}:")
                for (v, expr, _), c, b in zip(entries, cur_pe, best_pe):
                    if c != b:
                        flip = "FIX " if (b and not c) else "BREAK"
                        loc = (f"{v.file_section}:{v.line_number}:{v.name}"
                               + (f"[{v.aggregate_index}]"
                                  if v.aggregate_kind else ""))
                        print(f"      {flip}  {loc:<55}  {expr}")


if __name__ == "__main__":
    main()
