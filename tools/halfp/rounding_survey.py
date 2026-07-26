"""
Survey SP forward-conversion 1-ULP diffs to see whether they collapse
under a different DP->SP reduction rule.

Hypothesis: HAL/S-FC keeps SP-typed expressions in DP throughout
compile-time evaluation and applies a ROUNDING rule (rather than the
plain truncation the C driver currently uses) when storing the result
as SP.  If true, every borderline DP value whose dropped LSW is >=
0x80000000 should round UP, producing an SP value 1 ULP higher than
truncation.  Many "1 ULP off" entries should collapse.

Approach:
  1. Parse the dataset; for each SP entry with a source literal,
     evaluate the expression through halfp_driver to get the full 16-
     hex-digit DP intermediate result.
  2. For each entry, compute SP under several rules:
       trunc : current behavior (drop LSW)
       rhu   : round half up   (LSW MSB set -> +1 ULP, else trunc)
       rhe   : round half to even (RHU but ties go to LSB-even)
       rha   : round half away from zero (sign-aware)
       rhz   : round half toward zero == truncation (sanity baseline)
  3. For the entries that fail under trunc but match under one of the
     rounding rules, count the wins and break out by 1-ULP-only vs
     more.

Reads:  the parsed dataset (literalSCALARS-exp2.txt directly).
Writes: stdout report only.

Run from repo root:
    PYTHONPATH=. python3 tools/halfp/rounding_survey.py
"""

import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tools.halfp.parse_literal_scalars import parse_file
from tools.halfp.halsexpr import evaluate, EvalError, tokenize, collect_vars
from tools.halfp.halfp_runner import HalfpDriver
import json

DATASET = REPO_ROOT / "testCases/vagc#1296/literalSCALARS-exp2.txt"
DRIVER  = REPO_ROOT / "build/test/halfp_driver"
SIDECAR = REPO_ROOT / "tools/halfp/data/variable_values.json"


class _Drv:
    def __init__(self, drv): self.drv = drv
    def dp_from_string(self, s): return self.drv.dp_from_string(s)
    def dp_neg(self, h):         return self.drv.dp_neg(h)
    def dp_arith(self, op, a, b):return self.drv.dp_arith(op, a, b)


def reduce_sp(dp_hex: str, rule: str) -> str:
    """Return 8-char SP hex from a 16-char DP hex under `rule`.
    Tie = LSW exactly 0x80000000 (the dropped tail rounds the SP LSB
    halfway between two representable values)."""
    msw_int = int(dp_hex[:8], 16)
    lsw_int = int(dp_hex[8:], 16)

    if rule == "trunc" or rule == "rhz":
        return dp_hex[:8]

    if lsw_int == 0:                    # exact, no rounding choice
        return dp_hex[:8]

    # Halfway threshold for IBM hex DP-LSW: the dropped 32 bits act as
    # a fraction of the SP-mantissa LSB; halfway is 0x80000000.
    half  = 0x80000000
    above = lsw_int > half
    tie   = lsw_int == half

    def add_one_ulp_to_msw():
        # SP hex layout: top bit sign, next 7 bits char, low 24 bits mant.
        # +1 to the 24-bit mantissa, with carry into char.  Negative
        # values: incrementing magnitude means decrementing the integer
        # representation (sign bit stays).
        sign = msw_int & 0x80000000
        unsigned_mag = msw_int & 0x7FFFFFFF
        unsigned_mag += 1
        # Mantissa overflow into char field is fine; the test cases
        # we care about are all far from char boundaries.
        return f"{sign | unsigned_mag:08X}"

    def round_up(): return add_one_ulp_to_msw()
    def round_dn(): return dp_hex[:8].upper()

    if rule == "rhu":
        return round_up() if above or tie else round_dn()

    if rule == "rha":
        # Round half away from zero: same as RHU because magnitude
        # increases either direction.  Implementation already increments
        # magnitude via add_one_ulp_to_msw, so RHU == RHA for these.
        return round_up() if above or tie else round_dn()

    if rule == "rhe":
        if above: return round_up()
        if tie:
            # Tiebreak on LSB of SP mantissa: if LSB is 1, round up
            # to make it 0 (even); if 0, round down.
            sp_mant_lsb = msw_int & 1
            return round_up() if sp_mant_lsb == 1 else round_dn()
        return round_dn()

    raise ValueError(f"unknown rule {rule!r}")


def main():
    values = parse_file(str(DATASET))
    sidecar = json.loads(SIDECAR.read_text())

    var_map = {}
    for k, v in sidecar.items():
        if not k.startswith("_"):
            var_map[k] = v

    rules = ["trunc", "rhu", "rhe", "rha"]
    counts = {r: 0 for r in rules}                 # bit-exact matches
    counts_recovered = {r: 0 for r in rules}       # fixes a trunc miss
    one_ulp_total = 0                              # SP entries 1 ULP off under trunc
    by_rule_lsw_pattern = {r: Counter() for r in rules}

    sample_recovered = []   # entries trunc misses but `rhu` matches
    sample_still_off = []   # 1 ULP under trunc, still off under rhu

    # Cross-tab of the *discriminating* set (LSW != 0): for each entry,
    # tag whether trunc matches, RHU matches, or neither does, then count
    # by name-prefix, file-section, and a coarse expression shape.
    def _name_prefix(name: str) -> str:
        # CGCS_FOO_BAR -> CGCS  ;  GMT_LIFTOFF -> GMT  ;  Y -> Y
        return name.split("_", 1)[0] if "_" in name else name

    def _shape_class(literal: str) -> str:
        s = literal.strip()
        try:
            toks = tokenize(s)
        except EvalError:
            return "parse-err"
        if not toks: return "empty"
        # Single bare numeric literal
        if len(toks) == 1 and toks[0][0] == 'NUM':
            return "bare-num"
        # Leading '-' on a single numeric (e.g., "-0.5")
        if (len(toks) == 2 and toks[0] == ('OP', '-')
                and toks[1][0] == 'NUM'):
            return "neg-num"
        # Single identifier alias
        if len(toks) == 1 and toks[0][0] == 'ID':
            return "alias"
        # Detect operator presence
        ops = [t[1] for t in toks if t[0] == 'OP']
        has_id = any(t[0] == 'ID' for t in toks)
        has_paren = '(' in ops
        has_mul   = '*' in ops or any(  # implicit mul: NUM/ID adjacent
            i > 0 and toks[i-1][0] in ('NUM', 'ID', ) and t[0] in ('NUM', 'ID')
            for i, t in enumerate(toks)
        )
        has_div   = '/' in ops
        has_add   = '+' in ops
        has_sub   = '-' in ops
        # Slope-style: parens with exactly one '/' joining them
        if (s.count('(') >= 2 and has_div
                and ('-' in ops) and not has_add):
            return "slope-paren"
        # Single division of two atoms: "1/X", "A/B"
        if has_div and not has_add and not has_sub and not has_paren:
            return "atom-div"
        # Multiplication only (constant scaling like "80 X" or "X 0.5")
        if has_mul and not has_div and not has_add and not has_sub:
            return "scaled"
        # General formulas
        if has_paren and (has_add or has_sub or has_div):
            return "formula-paren"
        if has_add or has_sub:
            return "addsub"
        return "other"

    cross = {       # (bucket, prefix) -> count
        "trunc_only": Counter(),
        "rhu_only":   Counter(),
        "both_match": Counter(),
        "neither":    Counter(),
    }
    cross_section = {  # by file section
        "trunc_only": Counter(),
        "rhu_only":   Counter(),
        "both_match": Counter(),
        "neither":    Counter(),
    }
    cross_shape = {    # by expression shape
        "trunc_only": Counter(),
        "rhu_only":   Counter(),
        "both_match": Counter(),
        "neither":    Counter(),
    }

    with HalfpDriver(str(DRIVER)) as drv:
        wrap = _Drv(drv)
        for v in values:
            if v.type_class != "SP":
                continue
            if v.source_literal is None:
                continue
            # Auto-update var_map for aliases (single-identifier source).
            try:
                toks = tokenize(v.source_literal)
            except EvalError:
                continue
            try:
                full = evaluate(wrap, var_map, v.source_literal)
            except EvalError:
                continue
            # Refresh var_map alias entries (only the identifier-source case).
            if len(toks) == 1 and toks[0][0] == 'ID':
                var_map.setdefault(toks[0][1], full)
            if v.aggregate_size == 1 and v.storage_class == "SCALAR" and v.aggregate_kind is None:
                var_map.setdefault(v.name, full)

            target = v.hex.upper()
            trunc_sp = full[:8].upper()
            trunc_match = (trunc_sp == target)
            ulps_trunc = abs(int(trunc_sp, 16) - int(target, 16))
            rhu_sp = reduce_sp(full, "rhu").upper()
            rhu_match = (rhu_sp == target)

            for r in rules:
                got = reduce_sp(full, r).upper()
                if got == target:
                    counts[r] += 1
                    if not trunc_match:
                        counts_recovered[r] += 1

            # Cross-tab: only count entries where the rounding choice
            # actually matters (LSW != 0) — otherwise trunc and RHU are
            # identical and the entry doesn't discriminate.  Skip
            # asterisked entries: their binary is hand-patched, so a
            # mismatch tells us nothing about the compiler's rounding rule.
            if v.asterisked:
                continue
            lsw = int(full[8:], 16)
            if lsw != 0:
                if trunc_match and rhu_match:
                    bucket = "both_match"   # rare unless target is a tie
                elif trunc_match:
                    bucket = "trunc_only"
                elif rhu_match:
                    bucket = "rhu_only"
                else:
                    bucket = "neither"
                pref  = _name_prefix(v.name)
                shape = _shape_class(v.source_literal)
                cross[bucket][pref] += 1
                cross_section[bucket][v.file_section] += 1
                cross_shape[bucket][shape] += 1

            if not trunc_match and ulps_trunc == 1:
                one_ulp_total += 1
                # Track LSW pattern of the DP intermediate for these.
                lsw = int(full[8:], 16)
                lsw_class = (
                    "lsw=0" if lsw == 0 else
                    "lsw<0x80M" if lsw < 0x80000000 else
                    "lsw=0x80M" if lsw == 0x80000000 else
                    "lsw>0x80M"
                )
                for r in rules:
                    got = reduce_sp(full, r).upper()
                    if got == target:
                        by_rule_lsw_pattern[r][lsw_class] += 1
                rhu_got = reduce_sp(full, "rhu").upper()
                if rhu_got == target and len(sample_recovered) < 12:
                    sample_recovered.append(
                        (v.file_section, v.line_number, v.name, target,
                         trunc_sp, full, v.source_literal))
                elif rhu_got != target and len(sample_still_off) < 8:
                    sample_still_off.append(
                        (v.file_section, v.line_number, v.name, target,
                         trunc_sp, full, v.source_literal))

    print(f"=== Bit-exact match counts under each rule (SP entries only) ===")
    base = counts["trunc"]
    for r in rules:
        delta = counts[r] - base
        rec   = counts_recovered[r]
        sign  = "+" if delta >= 0 else ""
        print(f"  {r:<6}: {counts[r]:>6}  (vs trunc: {sign}{delta}, "
              f"of which {rec} are trunc-misses now matched)")

    print()
    print(f"=== 1-ULP trunc misses ({one_ulp_total}) — by LSW pattern ===")
    for r in rules:
        cls = by_rule_lsw_pattern[r]
        if not cls and r != "trunc":
            continue
        print(f"  {r:<6}: {dict(cls)}")

    print()
    print("=== Sample 1-ULP entries recovered by RHU ===")
    for sec, ln, name, tgt, tsp, dp, lit in sample_recovered[:8]:
        lsw = dp[8:]
        print(f"  {sec}:{ln}:{name}  target={tgt} trunc={tsp}  LSW={lsw}")
        print(f"    literal: {lit}")

    print()
    print("=== Sample 1-ULP entries still off under RHU ===")
    for sec, ln, name, tgt, tsp, dp, lit in sample_still_off[:8]:
        lsw = dp[8:]
        print(f"  {sec}:{ln}:{name}  target={tgt} trunc={tsp}  LSW={lsw}")
        print(f"    literal: {lit}")

    # ---- Cross-tabs over the discriminating set (LSW != 0) ----
    def _print_cross(title, table, top_n=20):
        # Collect all keys, compute totals per key, sort by total descending.
        keys = set()
        for d in table.values():
            keys.update(d.keys())
        rows = []
        for k in keys:
            total = sum(table[b].get(k, 0) for b in table)
            rows.append((k, total,
                         table["trunc_only"].get(k, 0),
                         table["rhu_only"].get(k, 0),
                         table["both_match"].get(k, 0),
                         table["neither"].get(k, 0)))
        rows.sort(key=lambda r: -r[1])
        print()
        print(f"=== {title} (discriminating set; LSW != 0) ===")
        print(f"{'key':<20}  {'total':>6}  {'trunc':>6}  {'rhu':>6}  "
              f"{'both':>6}  {'neither':>7}  {'rhu%':>6}")
        for k, tot, t, r, b, n in rows[:top_n]:
            disc = t + r  # cases where rule matters and one of them matches
            pct  = (r / disc * 100) if disc else 0.0
            print(f"{k:<20}  {tot:>6}  {t:>6}  {r:>6}  {b:>6}  {n:>7}  "
                  f"{pct:>5.1f}%")
        if len(rows) > top_n:
            print(f"  ... and {len(rows) - top_n} more keys")

    _print_cross("By name prefix",      cross)
    _print_cross("By file section",     cross_section)
    _print_cross("By expression shape", cross_shape)


if __name__ == "__main__":
    main()
