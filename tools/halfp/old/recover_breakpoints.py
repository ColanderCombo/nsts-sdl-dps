"""
Recover values for HAL/S table-breakpoint identifiers that aren't
defined anywhere in the dataset, by inverting slope/intercept
expressions whose binary target is known.

Reads `build/test/unresolved_for_var_recovery.json` and updates the
sidecar `tools/halfp/variable_values.json` with newly recovered values
under a `_recovered_from_breakpoints` group.

Patterns handled (covers nearly all entries in the deviations file):

  S2  : A - B / C - D                    -> (A - B) / (C - D)              (slope)
  S6  : A - B C - D / E - F              -> A - B * (C - D) / (E - F)      (intercept)
  S3M : A - B C                          -> A - B * C                      (3-token "intercept" w/ explicit slope)
  M2  : A B                              -> A * B
  Q2  : A / B                            -> A / B
  CST : <numeric literal>                -> evaluated directly

Strategy: fixed-point iteration. Each pass:
  1. For every unresolved expression with exactly one missing variable,
     algebraically solve for that variable using the known values, the
     pattern shape, and the target hex.
  2. Verify by re-evaluating the parenthesized form through the C
     halfp_driver and comparing bytes against target_hex.
  3. Add the solved variable to the working sidecar and re-scan.
Loop until no more progress is made.

Conflicts (a variable solved from two different expressions yielding
different values, even after rounding to SP) are flagged and NOT
written.

Run from repo root:
    PYTHONPATH=. python3 tools/halfp/recover_breakpoints.py
"""

import json
import math
import re
import sys
from pathlib import Path
from collections import defaultdict

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from tools.halfp.halsexpr import evaluate, EvalError, tokenize
from tools.halfp.halfp_runner import HalfpDriver


UNRESOLVED_PATH = REPO_ROOT / "build/test/halfp/unresolved_for_var_recovery.json"
SIDECAR_PATH    = REPO_ROOT / "tools/halfp/data/variable_values.json"
DRIVER_PATH     = REPO_ROOT / "build/test/halfp_driver"


# ---------- Pattern recognition ------------------------------------

# Each pattern returns (kind, [tokens]) where kind picks the inversion
# routine, and tokens are the operand strings (variable names or
# numeric literals).

ID = r'[A-Za-z_][A-Za-z0-9_]*'
NUM = r'[+-]?\d+\.?\d*(?:[Ee][+-]?\d+)?|[+-]?\.\d+(?:[Ee][+-]?\d+)?'
TOK = rf'(?:{ID}|{NUM})'


def classify(expr: str):
    e = expr.strip()

    # S6: 'A - B C - D / E - F'   intercept = A - B * (C - D) / (E - F)
    m = re.fullmatch(
        rf'\s*({TOK})\s*-\s*({TOK})\s+({TOK})\s*-\s*({TOK})\s*/\s*({TOK})\s*-\s*({TOK})\s*',
        e)
    if m:
        return ('S6', list(m.groups()))

    # S2: 'A - B / C - D'   slope = (A - B) / (C - D)
    m = re.fullmatch(
        rf'\s*({TOK})\s*-\s*({TOK})\s*/\s*({TOK})\s*-\s*({TOK})\s*',
        e)
    if m:
        return ('S2', list(m.groups()))

    # S3M: 'A - B C'   intercept w/ explicit slope = A - B * C
    m = re.fullmatch(
        rf'\s*({TOK})\s*-\s*({TOK})\s+({TOK})\s*',
        e)
    if m:
        return ('S3M', list(m.groups()))

    # Q2: 'A / B'  (also matches 'X/Y')
    m = re.fullmatch(rf'\s*({TOK})\s*/\s*({TOK})\s*', e)
    if m:
        return ('Q2', list(m.groups()))

    # M2: 'A B'  (space-multiplication)
    m = re.fullmatch(rf'\s*({TOK})\s+({TOK})\s*', e)
    if m:
        return ('M2', list(m.groups()))

    # Bare numeric
    m = re.fullmatch(rf'\s*({NUM})\s*', e)
    if m:
        return ('CST', [m.group(1)])

    return (None, None)


def paren_form(kind, toks):
    """Return the canonical parenthesized expression string."""
    if kind == 'S2':
        a, b, c, d = toks
        return f"({a} - {b}) / ({c} - {d})"
    if kind == 'S6':
        a, b, c, d, e, f = toks
        return f"{a} - {b} * ({c} - {d}) / ({e} - {f})"
    if kind == 'S3M':
        a, b, c = toks
        return f"{a} - {b} * {c}"
    if kind == 'Q2':
        a, b = toks
        return f"{a} / {b}"
    if kind == 'M2':
        a, b = toks
        return f"{a} * {b}"
    if kind == 'CST':
        return toks[0]
    raise ValueError(kind)


# ---------- Solver helpers -----------------------------------------

class Drv:
    """Thin facade around HalfpDriver matching halsexpr.Evaluator's
    expected interface (dp_from_string, dp_neg, dp_arith)."""
    def __init__(self, drv):
        self.drv = drv
    def dp_from_string(self, s): return self.drv.dp_from_string(s)
    def dp_neg(self, h):         return self.drv.dp_neg(h)
    def dp_arith(self, op, a, b):return self.drv.dp_arith(op, a, b)


def is_numeric_tok(t: str) -> bool:
    return bool(re.fullmatch(NUM, t.strip()))


def value_of_tok(drv, var_map, t: str):
    """Return 16-char DP hex for either a numeric literal or a
    variable looked up in var_map. Returns None if it's a variable
    not in the map."""
    t = t.strip()
    if is_numeric_tok(t):
        return drv.dp_from_string(t)
    return var_map.get(t)


def hex_to_float(drv, hex16: str) -> float:
    return float(drv._exchange("D " + hex16))


def float_to_dp_hex(drv, value: float) -> str:
    if math.isnan(value) or math.isinf(value):
        return None
    return drv.dp_from_string(f"{value:.17g}")


def target_as_dp_hex(target_hex: str, type_class: str) -> str:
    if type_class == 'SP':
        return (target_hex + '00000000')[:16]
    return target_hex


def hex_eq(actual_dp_hex: str, target_hex: str, type_class: str) -> bool:
    if type_class == 'SP':
        return actual_dp_hex[:8] == target_hex
    return actual_dp_hex == target_hex


# ---------- Per-pattern inverter -----------------------------------

def solve_one(drv, kind, toks, var_map, target_hex, type_class):
    """If exactly one operand is unresolvable from var_map (and isn't
    a numeric literal), solve for it. Returns (var_name, dp_hex_value)
    or None.

    The shape of the target hex (SP -> 8 chars, DP -> 16) tells us how
    much precision we have. We solve in Python float (53-bit mantissa,
    matches IBM DP's 56-bit closely enough for our purposes), then
    re-render the value as IBM DP, and the verification pass below
    catches any mismatch.
    """
    # Resolve every token to either a hex value or "<missing-name>".
    resolved = []
    missing_idx = None
    missing_name = None
    for i, t in enumerate(toks):
        if is_numeric_tok(t):
            resolved.append(('LIT', t, drv.dp_from_string(t)))
            continue
        v = var_map.get(t)
        if v is None:
            if missing_idx is not None:
                # Two missing -> can't solve here (caller already filtered).
                return None
            missing_idx = i
            missing_name = t
            resolved.append(('MISS', t, None))
        else:
            resolved.append(('VAR', t, v))

    if missing_idx is None:
        return None  # nothing to solve

    # Get float values for all the non-missing operands.
    fvals = [None]*len(resolved)
    for i, (k, t, h) in enumerate(resolved):
        if k != 'MISS':
            fvals[i] = hex_to_float(drv, h)

    target_f = hex_to_float(drv, target_as_dp_hex(target_hex, type_class))

    # Pattern-specific inversion.
    try:
        if kind == 'S2':
            # target = (a - b) / (c - d); solve for the missing
            a, b, c, d = fvals
            if missing_idx == 0:    # A = target*(C-D) + B
                solved = target_f * (c - d) + b
            elif missing_idx == 1:  # B = A - target*(C-D)
                solved = a - target_f * (c - d)
            elif missing_idx == 2:  # C = (A-B)/target + D
                if target_f == 0:
                    return None
                solved = (a - b) / target_f + d
            elif missing_idx == 3:  # D = C - (A-B)/target
                if target_f == 0:
                    return None
                solved = c - (a - b) / target_f
            else:
                return None
        elif kind == 'S6':
            # target = a - b*(c-d)/(e-f)
            a, b, c, d, e, f = fvals
            if missing_idx == 0:    # A = target + b*(c-d)/(e-f)
                if e == f: return None
                solved = target_f + b * (c - d) / (e - f)
            elif missing_idx == 1:  # B = (A - target)*(E-F)/(C-D)
                if c == d: return None
                solved = (a - target_f) * (e - f) / (c - d)
            elif missing_idx == 2:  # C = (A - target)*(E-F)/B + D
                if b == 0 or e == f: return None
                solved = (a - target_f) * (e - f) / b + d
            elif missing_idx == 3:  # D = C - (A - target)*(E-F)/B
                if b == 0 or e == f: return None
                solved = c - (a - target_f) * (e - f) / b
            elif missing_idx == 4:  # E = b*(C-D)/(A-target) + F
                if a == target_f: return None
                solved = b * (c - d) / (a - target_f) + f
            elif missing_idx == 5:  # F = E - b*(C-D)/(A-target)
                if a == target_f: return None
                solved = e - b * (c - d) / (a - target_f)
            else:
                return None
        elif kind == 'S3M':
            # target = a - b*c
            a, b, c = fvals
            if missing_idx == 0:    # A = target + b*c
                solved = target_f + b * c
            elif missing_idx == 1:  # B = (a - target)/c
                if c == 0: return None
                solved = (a - target_f) / c
            elif missing_idx == 2:  # C = (a - target)/b
                if b == 0: return None
                solved = (a - target_f) / b
            else:
                return None
        elif kind == 'Q2':
            a, b = fvals
            if missing_idx == 0:    # A = target * B
                solved = target_f * b
            elif missing_idx == 1:  # B = A / target
                if target_f == 0: return None
                solved = a / target_f
            else:
                return None
        elif kind == 'M2':
            a, b = fvals
            if missing_idx == 0:    # A = target / B
                if b == 0: return None
                solved = target_f / b
            elif missing_idx == 1:  # B = target / A
                if a == 0: return None
                solved = target_f / a
            else:
                return None
        else:
            return None
    except (ZeroDivisionError, OverflowError):
        return None

    solved_hex = float_to_dp_hex(drv, solved)
    if solved_hex is None:
        return None
    return (missing_name, solved_hex)


def verify_solution(drv, paren_expr, var_map, candidate, target_hex, type_class):
    """Plug `candidate` (name->hex) into a copy of var_map and
    evaluate `paren_expr`. Returns (ok, actual_dp_hex)."""
    name, hexval = candidate
    test_map = {**var_map, name: hexval}
    try:
        got = evaluate(Drv(drv), test_map, paren_expr)
    except EvalError:
        return (False, None)
    return (hex_eq(got, target_hex, type_class), got)


def try_relax_to_match(drv, kind, toks, var_map, target_hex, type_class,
                       missing_name, initial_hex, paren_expr):
    """If verification fails by a tiny amount, attempt ULP
    perturbations around the candidate (walking up and down in DP)
    until one re-evaluates to the target hex. SP-targeted entries
    have a wide acceptance window because many DP values round to
    the same 32-bit SP, so we walk further in that case.
    Returns hex string on success, else None."""
    paren = paren_expr
    val = hex_to_float(drv, initial_hex)
    if val == 0.0:
        # Walk ULPs around zero in both directions.
        candidates = [0.0,
                      math.nextafter(0.0,  math.inf),
                      math.nextafter(0.0, -math.inf)]
    else:
        # Walk N ULPs up and down; SP needs ~10^7 width worst case
        # but the DP candidate is already very close so a few hundred
        # ULPs suffices.
        n_walk = 1024 if type_class == 'SP' else 16
        candidates = [val]
        up, dn = val, val
        for _ in range(n_walk):
            up = math.nextafter(up, math.inf)
            dn = math.nextafter(dn, -math.inf)
            candidates.append(up); candidates.append(dn)

    seen = set()
    for cand in candidates:
        h = float_to_dp_hex(drv, cand)
        if h is None or h in seen:
            continue
        seen.add(h)
        ok, _ = verify_solution(drv, paren, var_map,
                                (missing_name, h),
                                target_hex, type_class)
        if ok:
            return h
    return None


# ---------- Main loop ----------------------------------------------

def main():
    data = json.loads(UNRESOLVED_PATH.read_text())
    sidecar = json.loads(SIDECAR_PATH.read_text())
    unresolved = data['unresolved']

    # Build initial var map from var_map_at_end (full known map)
    # plus existing sidecar (sidecar already merged in var_map_at_end
    # but kept here for safety).
    full_var_map = dict(data['var_map_at_end'])

    recovered = {}            # name -> hex (newly solved)
    conflicts = defaultdict(list)  # name -> list of (loc, hex)
    flagged = []              # (loc, reason)

    # Pre-classify each entry once.
    classified = []
    for idx, e in enumerate(unresolved):
        kind, toks = classify(e['expression_to_eval'])
        classified.append({
            'idx': idx,
            'kind': kind,
            'toks': toks,
            'entry': e,
        })

    pattern_hist = defaultdict(int)
    for c in classified:
        pattern_hist[c['kind']] += 1
    print(f"Pattern histogram: {dict(pattern_hist)}")

    # Pre-index classified entries by pattern, for the coupled-pair pass.
    by_kind = defaultdict(list)
    for c in classified:
        by_kind[c['kind']].append(c)

    with HalfpDriver(str(DRIVER_PATH)) as drv:
        progress = True
        iteration = 0
        while progress:
            iteration += 1
            progress = False
            for c in classified:
                kind = c['kind']
                if kind is None:
                    continue
                e = c['entry']
                toks = c['toks']
                target_hex = e['target_hex']
                type_class = e['type_class']

                # Determine current "missing" tokens (non-literal,
                # not in full_var_map and not yet recovered).
                effective_map = {**full_var_map, **recovered}
                missing = [t for t in toks
                           if not is_numeric_tok(t) and t not in effective_map]

                if len(missing) == 0:
                    continue
                if len(missing) > 1:
                    continue

                result = solve_one(drv, kind, toks, effective_map,
                                   target_hex, type_class)
                if result is None:
                    continue
                name, hexval = result
                paren = paren_form(kind, toks)

                # Verify by round-trip evaluation.
                ok, got = verify_solution(drv, paren, effective_map,
                                          (name, hexval),
                                          target_hex, type_class)

                if not ok:
                    # Try ULP-perturbation to land on exact target.
                    refined = try_relax_to_match(
                        drv, kind, toks, effective_map,
                        target_hex, type_class, name, hexval, paren)
                    if refined is not None:
                        hexval = refined
                        ok = True

                if not ok:
                    flagged.append((e['location'], kind, name,
                                    f'verify failed: got={got} want={target_hex}'))
                    continue

                # Conflict check: same name solved before, must agree.
                # SP precision: only top 8 chars matter for downstream
                # consumers, but we've stored DP hex; allow agreement
                # at SP level if type_class is SP.
                if name in recovered and recovered[name] != hexval:
                    # Compare via float to give friendlier rounding,
                    # but the SP downstream will only see 8-char hex.
                    prev = recovered[name]
                    # Compute SP top-8 of each.
                    if hexval[:8] == prev[:8]:
                        # Same to SP precision; keep original (first wins).
                        pass
                    else:
                        conflicts[name].append((e['location'], hexval))
                        continue
                else:
                    if name not in recovered:
                        progress = True
                    recovered[name] = hexval

            # ---------- Coupled slope+intercept pair pass ----------
            # For each S2 (slope) entry, find a paired S6 (intercept)
            # entry sharing the same A1/A2/B1/B2 (canonical form S6
            # toks = [A1, B1, A2, A1, B2, B1]). If exactly two of
            # {A1, A2, B1, B2} are unknown, use both equations:
            #     Slope:     (A2-A1)/(B2-B1) = S
            #     Intercept: A1 - B1*S       = I  (since S=target_slope)
            # This yields four solvable subcases (A1+A2 missing,
            # A2+B1 missing, A1+B2 missing, B1+B2 missing) and two
            # under-determined subcases (A1+B1, A2+B2).
            for c_slope in by_kind['S2']:
                e = c_slope['entry']
                toks = c_slope['toks']
                if len(toks) != 4: continue
                a2, a1, b2, b1 = toks
                # Skip numeric-token slopes.
                if any(is_numeric_tok(t) for t in toks):
                    continue
                # Already resolved?
                eff = {**full_var_map, **recovered}
                missing_slope = [t for t in toks if t not in eff]
                if len(missing_slope) != 2: continue
                # Find a matching intercept (S6) whose toks are
                # [A1, B1, A2, A1, B2, B1] (canonical form).
                pair = None
                for c_int in by_kind['S6']:
                    it_toks = c_int['toks']
                    if len(it_toks) != 6: continue
                    if (it_toks[0] == a1 and it_toks[1] == b1
                        and it_toks[2] == a2 and it_toks[3] == a1
                        and it_toks[4] == b2 and it_toks[5] == b1):
                        pair = c_int; break
                if pair is None: continue
                e_int = pair['entry']
                target_slope = e['target_hex']
                target_int = e_int['target_hex']
                tc_slope = e['type_class']
                tc_int = e_int['type_class']

                S = hex_to_float(drv,
                    target_as_dp_hex(target_slope, tc_slope))
                I = hex_to_float(drv,
                    target_as_dp_hex(target_int, tc_int))

                # Identify which variables are unknown.
                miss_set = set(missing_slope)
                # Skip under-determined cases.
                if miss_set in ({a1, b1}, {a2, b2}):
                    continue

                # Compute the unknowns based on which two are missing.
                A1 = hex_to_float(drv, eff[a1]) if a1 not in miss_set else None
                A2 = hex_to_float(drv, eff[a2]) if a2 not in miss_set else None
                B1 = hex_to_float(drv, eff[b1]) if b1 not in miss_set else None
                B2 = hex_to_float(drv, eff[b2]) if b2 not in miss_set else None

                if S == 0: continue

                try:
                    if miss_set == {b1, b2}:
                        # Case 2: A1, A2 known. B1 = (A1 - I)/S; B2 = (A2-A1)/S + B1
                        B1 = (A1 - I) / S
                        B2 = (A2 - A1) / S + B1
                    elif miss_set == {a2, b1}:
                        # Case 3: A1, B2 known. B1 from intercept; A2 = S*(B2-B1) + A1
                        B1 = (A1 - I) / S
                        A2 = S * (B2 - B1) + A1
                    elif miss_set == {a1, b2}:
                        # Case 4: A2, B1 known. A1 = I + B1*S; B2 = (A2-A1)/S + B1
                        A1 = I + B1 * S
                        B2 = (A2 - A1) / S + B1
                    elif miss_set == {a1, a2}:
                        # Case 6: B1, B2 known. A1 = I + B1*S; A2 = S*(B2-B1) + A1
                        A1 = I + B1 * S
                        A2 = S * (B2 - B1) + A1
                    else:
                        continue
                except (ZeroDivisionError, OverflowError):
                    continue

                B1_hex0 = float_to_dp_hex(drv, B1) if b1 in miss_set else eff[b1]
                B2_hex0 = float_to_dp_hex(drv, B2) if b2 in miss_set else eff[b2]
                A1_hex0 = float_to_dp_hex(drv, A1) if a1 in miss_set else eff[a1]
                A2_hex0 = float_to_dp_hex(drv, A2) if a2 in miss_set else eff[a2]

                # Verify both. Must match BOTH slope and intercept
                # parenthesized forms.
                paren_slope = paren_form('S2', toks)
                paren_int = paren_form('S6', pair['toks'])

                test_map = {**eff,
                            a1: A1_hex0, a2: A2_hex0,
                            b1: B1_hex0, b2: B2_hex0}
                try:
                    rs = evaluate(Drv(drv), test_map, paren_slope)
                    ri = evaluate(Drv(drv), test_map, paren_int)
                    ok_slope = hex_eq(rs, target_slope, tc_slope)
                    ok_int = hex_eq(ri, target_int, tc_int)
                except EvalError:
                    ok_slope = ok_int = False

                if not (ok_slope and ok_int):
                    # Walk one of the missing values via ULPs and
                    # re-derive the other (which is uniquely determined
                    # by the slope equation given the first).
                    found = None
                    n_walk = 1024 if (tc_slope == 'SP' or tc_int == 'SP') else 64
                    # Pick the "primary" walked variable — the one
                    # that's in the intercept (so its perturbation
                    # affects the intercept's targeting precision).
                    # Then compute the "secondary" from the slope eq.
                    if b1 in miss_set:
                        primary, secondary = 'b1', 'b2'
                        primary_val = B1; secondary_init = B2
                    elif a1 in miss_set:
                        primary, secondary = 'a1', 'a2'
                        primary_val = A1; secondary_init = A2
                    elif a2 in miss_set:
                        primary, secondary = 'a2', 'b2'
                        primary_val = A2; secondary_init = B2
                    elif b2 in miss_set:
                        primary, secondary = 'b2', 'a2'
                        primary_val = B2; secondary_init = A2
                    else:
                        primary = secondary = None

                    if primary is None:
                        continue

                    cands = [primary_val]
                    up, dn = primary_val, primary_val
                    for _ in range(n_walk):
                        up = math.nextafter(up, math.inf)
                        dn = math.nextafter(dn, -math.inf)
                        cands.append(up); cands.append(dn)

                    seen = set()
                    for cand in cands:
                        h_p = float_to_dp_hex(drv, cand)
                        if h_p is None or h_p in seen:
                            continue
                        seen.add(h_p)

                        # Recompute the dependent variable from
                        # the slope equation, given the perturbed
                        # primary value (and the other knowns).
                        try:
                            if primary == 'b1':
                                cand_A1 = A1; cand_A2 = A2
                                cand_B1 = cand
                                cand_B2 = (cand_A2 - cand_A1) / S + cand_B1
                            elif primary == 'a1':
                                cand_B1 = B1; cand_B2 = B2
                                cand_A1 = cand
                                cand_A2 = S * (cand_B2 - cand_B1) + cand_A1
                            elif primary == 'a2':
                                cand_A1 = A1; cand_B2 = B2
                                cand_A2 = cand
                                cand_B1 = cand_B2 - (cand_A2 - cand_A1)/S
                            elif primary == 'b2':
                                cand_A1 = A1; cand_B1 = B1
                                cand_B2 = cand
                                cand_A2 = S*(cand_B2 - cand_B1) + cand_A1
                        except (ZeroDivisionError, OverflowError):
                            continue

                        # Apply ULP search around secondary too.
                        sec_init = {'a1': cand_A1, 'a2': cand_A2,
                                    'b1': cand_B1, 'b2': cand_B2}[secondary]
                        sec_cands = [sec_init]
                        up2, dn2 = sec_init, sec_init
                        for _ in range(128):
                            up2 = math.nextafter(up2, math.inf)
                            dn2 = math.nextafter(dn2, -math.inf)
                            sec_cands.append(up2); sec_cands.append(dn2)
                        for sc in sec_cands:
                            h_s = float_to_dp_hex(drv, sc)
                            if h_s is None: continue
                            tm = dict(eff)
                            # Set primary
                            tm[{'b1': b1, 'b2': b2, 'a1': a1, 'a2': a2}[primary]] = h_p
                            tm[{'b1': b1, 'b2': b2, 'a1': a1, 'a2': a2}[secondary]] = h_s
                            try:
                                rs = evaluate(Drv(drv), tm, paren_slope)
                                ri = evaluate(Drv(drv), tm, paren_int)
                            except EvalError:
                                continue
                            if (hex_eq(rs, target_slope, tc_slope) and
                                hex_eq(ri, target_int, tc_int)):
                                # Bind back A1/A2/B1/B2 hexes.
                                if primary == 'b1':
                                    B1_hex0, B2_hex0 = h_p, h_s
                                elif primary == 'a1':
                                    A1_hex0, A2_hex0 = h_p, h_s
                                elif primary == 'a2':
                                    A2_hex0, B1_hex0 = h_p, h_s
                                elif primary == 'b2':
                                    B2_hex0, A2_hex0 = h_p, h_s
                                found = True
                                break
                        if found: break
                    if not found:
                        continue

                # Conflict / new-recovery checks for the missing names.
                pairs_to_record = []
                if a1 in miss_set: pairs_to_record.append((a1, A1_hex0))
                if a2 in miss_set: pairs_to_record.append((a2, A2_hex0))
                if b1 in miss_set: pairs_to_record.append((b1, B1_hex0))
                if b2 in miss_set: pairs_to_record.append((b2, B2_hex0))
                for nm, hv in pairs_to_record:
                    if nm in recovered and recovered[nm] != hv:
                        if recovered[nm][:8] == hv[:8]:
                            pass
                        else:
                            conflicts[nm].append((e['location'], hv))
                            continue
                    else:
                        if nm not in recovered:
                            progress = True
                        recovered[nm] = hv

            print(f"  iter {iteration}: recovered={len(recovered)} "
                  f"flagged={len(flagged)} conflicts={len(conflicts)}")

        # After convergence, count how many unresolved entries become
        # newly resolvable (zero missing variables).
        effective_map = {**full_var_map, **recovered}
        newly_resolvable = 0
        still_blocked = []
        for c in classified:
            e = c['entry']
            toks = c['toks']
            kind = c['kind']
            if kind is None:
                still_blocked.append((e['location'], 'unknown pattern'))
                continue
            missing = [t for t in toks
                       if not is_numeric_tok(t) and t not in effective_map]
            if not missing:
                newly_resolvable += 1
            else:
                still_blocked.append((e['location'], f'missing {missing}'))

    # Collect forward-referenced names that ARE defined later in the
    # file (present in var_map_at_end). These need to be in the sidecar
    # to short-circuit forward-reference failures at expression-eval
    # time. The test framework treats sidecar as a default that file
    # walking can override (last-write-wins, file precedence), so this
    # is safe.
    forward_refs = {}
    for c in classified:
        e = c['entry']
        for name in e.get('vars_missing', []):
            if name in full_var_map and name not in forward_refs:
                forward_refs[name] = full_var_map[name]

    # ---------- Write sidecar ----------
    if recovered or forward_refs:
        sidecar = json.loads(SIDECAR_PATH.read_text())  # reload fresh
        sidecar.setdefault(
            "_recovered_from_breakpoints",
            "Auto-recovered by tools/halfp/recover_breakpoints.py: "
            "table-breakpoint identifiers (CGCK_T*1_1, CGCK_T*1_2, "
            "TF, GRPHI, TDAXFD*, ...) solved by inverting slope and "
            "intercept expressions whose IBM-DP target hex is known. "
            "Each value was round-trip verified against the binary's "
            "target (SP: top-8-char match; DP: full-hex match)."
        )
        # First, recovered entries (truly unknown until inversion).
        for name, hexval in sorted(recovered.items()):
            sidecar[name] = hexval
        # Then, forward-reference pre-fills: variables that ARE
        # defined later in the dataset but are referenced earlier.
        # Pre-loading them in the sidecar lets the test's expression
        # evaluator resolve those references at parse time; the
        # file walk's later definition overrides with the same value.
        sidecar.setdefault(
            "_forward_refs_prefill",
            "File-defined breakpoint values pre-populated here so "
            "the test's in-order file walk can resolve forward "
            "references (an expression at line N may reference a "
            "variable first defined at line N+K). The same value "
            "is re-asserted with origin='file' when the walk reaches "
            "the definition site."
        )
        for name, hexval in sorted(forward_refs.items()):
            if name not in sidecar:
                sidecar[name] = hexval

        SIDECAR_PATH.write_text(json.dumps(sidecar, indent=2) + "\n")

    print(f"  forward-ref prefills: {len(forward_refs)}")

    # ---------- Summary ----------
    print()
    print(f"=== Summary ===")
    print(f"Pattern hist:        {dict(pattern_hist)}")
    print(f"Variables recovered: {len(recovered)}")
    print(f"Newly resolvable entries: {newly_resolvable}/{len(unresolved)}")
    print(f"Still blocked: {len(still_blocked)}")
    print(f"Conflicts: {len(conflicts)}")
    print(f"Verify-flagged: {len(flagged)}")

    if conflicts:
        print()
        print("--- Conflicts (NOT written) ---")
        for name, lst in sorted(conflicts.items()):
            print(f"  {name}: {lst}")

    if flagged:
        print()
        print("--- Verify-flagged (NOT written) ---")
        for loc, kind, name, why in flagged[:30]:
            print(f"  {loc}  [{kind}] {name}: {why}")
        if len(flagged) > 30:
            print(f"  ... and {len(flagged)-30} more")

    if still_blocked:
        print()
        print("--- Still blocked (sample) ---")
        for loc, why in still_blocked[:20]:
            print(f"  {loc}: {why}")
        if len(still_blocked) > 20:
            print(f"  ... and {len(still_blocked)-20} more")

    print()
    print(f"Wrote {SIDECAR_PATH}")
    return recovered, conflicts, flagged, still_blocked


if __name__ == "__main__":
    main()
