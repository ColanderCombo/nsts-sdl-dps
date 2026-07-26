"""
Reformat literalSCALARS-exp2.txt using OUR computed values, in the same
line/column structure as the input, so a `diff` against the original
file shows exactly where our forward/reverse conversions disagree with
the binary or with MAFGEN.

For each scalar slot:
  * Hex column gets `dp_from_string(source_literal)` (= "what we'd put
    in the binary if we were the compiler"), or the original binary if
    no source literal is available.
  * Decimal column gets `dp_to_string(our_hex)` (= our renderer applied
    to our forward-converted hex), so the file is internally consistent:
    decimal is the round-trip of the hex we just emitted.  A `diff`
    against the input shows both hex disagreements and renderer
    disagreements, against a single self-consistent baseline.
  * Asterisks are preserved per-halfword from the input.  Our hex value
    differs from MAFGEN's hex on every asterisked halfword (since our
    forward conversion doesn't see the hand-patch), so leaving the `*`
    in place flags the diff as "expected divergence — hand-patched".

Section headers (`[DASS_*.ASC]`), blank lines, and any unrecognized
line shape are passed through unchanged.

Run from repo root:
    PYTHONPATH=. python3 tools/halfp/reformat_dataset.py [<output-path>]
"""

import json
import re
import sys
from pathlib import Path
from collections import defaultdict

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tools.halfp.parse_literal_scalars import (
    parse_lines, FPValue, _TYPE_TRIPLET, _HEX_TOKEN, _DECIMAL_TOKEN,
)
from tools.halfp.halsexpr import (
    evaluate as eval_halexpr, EvalError, tokenize,
)
from tools.halfp.halfp_runner import HalfpDriver

INPUT_PATH    = REPO_ROOT / "testCases/vagc#1296/literalSCALARS-exp2.txt"
SIDECAR_PATH  = REPO_ROOT / "tools/halfp/data/variable_values.json"
DRIVER_PATH   = REPO_ROOT / "build/test/halfp_driver"
DEFAULT_OUT   = REPO_ROOT / "build/test/halfp/literalSCALARS-exp2.our.txt"


class _D:
    def __init__(self, drv): self.drv = drv
    def dp_from_string(self, s): return self.drv.dp_from_string(s)
    def dp_neg(self, h):         return self.drv.dp_neg(h)
    def dp_arith(self, op, a, b):return self.drv.dp_arith(op, a, b)


def _our_hex_for(v: FPValue, drv, wrap, var_map) -> str:
    """Forward-convert v.source_literal to hex.  Falls back to v.hex
    when there's no usable source literal or evaluation fails."""
    if not v.source_literal:
        return v.hex
    try:
        full = eval_halexpr(wrap, var_map, v.source_literal)
    except EvalError:
        return v.hex
    if v.type_class == "SP":
        return full[:8].upper()
    return full.upper()


def _our_decimal_for(v: FPValue, our_hex: str, drv) -> str:
    """Render OUR forward-converted hex through dp_to_string.  Pairs
    with `_our_hex_for` so the .our file is self-consistent: the
    decimal column is exactly what our renderer would produce for the
    hex column we just emitted.  The input file reserves col 56 as a
    sign slot ('-' for negative, ' ' for positive or zero), with digits
    starting at col 57; we mirror that here."""
    if v.type_class == "SP":
        dec = drv.sp_to_string(our_hex)
    else:
        dec = drv.dp_to_string(our_hex)
    if dec and not dec.startswith('-'):
        dec = ' ' + dec
    return dec


def _fmt_halfwords(hex_str: str) -> list[str]:
    """Split an 8-char (SP) or 16-char (DP) hex into 4-char halfwords."""
    h = hex_str.upper()
    return [h[i:i+4] for i in range(0, len(h), 4)]


def _ast_slot(asterisked: bool) -> str:
    return '*' if asterisked else ' '


def _emit_header_hex_zone(our_hexes: list[str], values: list[FPValue]) -> str:
    """Format the hex zone (cols 32..55, width 24) for a single-value
    entry.  Col 32 is the asterisk slot for halfword 1, so hex digits
    begin at col 33.  Asterisk slots between halfwords mark hand-patched
    positions in the input and are preserved here.
    SP: '<*>XXXX <*>XXXX' (11 chars body + 13 padding).
    DP: '<*>XXXX <*>XXXX <*>XXXX <*>XXXX' (23 chars body + 1 padding)."""
    if len(values) != 1:
        return " " * 24   # array header: blank hex zone
    v = values[0]
    hw = _fmt_halfwords(our_hexes[0])
    asts = v.halfword_asterisks or tuple([False] * len(hw))
    parts = [_ast_slot(asts[i]) + hw[i] for i in range(len(hw))]
    body = " ".join(parts)        # SP=11 chars, DP=23 chars
    return body + " " * (24 - len(body))


def _emit_header_decimal_zone(our_hexes: list[str], values: list[FPValue], drv) -> str:
    """Format the decimal zone (cols 56..80, width 25).  Three cases:
    single value -> rendered decimal; ARRAY -> 'ARRAY' marker near the
    right end so the type triplet stays at col 81; non-ARRAY multi-value
    (VECTORs) -> all blank."""
    if len(values) > 1:
        if values[0].aggregate_kind == "ARRAY":
            # 'ARRAY' at cols 74..78, then 2 trailing spaces.
            return f"{'':<18}ARRAY  "
        else:
            # SP/DP VECTOR (3-component): no 'ARRAY' marker.
            return " " * 25
    v = values[0]
    dec = _our_decimal_for(v, our_hexes[0], drv)
    return f"{dec:<25}"


# Trailing-whitespace target: input continuation lines end at col 100
# (98 chars body + 2 trailing spaces).  We mirror that.
LINE_WIDTH = 100


def _emit_array_hex_lines(our_hexes: list[str], values: list[FPValue]) -> list[str]:
    """Emit continuation hex lines for an ARRAY.  Input format: 3-space
    indent + per-halfword <ast-slot><4-hex>, halfwords separated by 1
    space (so the slot before halfword N+1 sits at col 3 + 6N).
    Asterisk slots are preserved from each value's halfword_asterisks."""
    # Flatten halfwords paired with their asterisk markers.
    pairs: list[tuple[str, str]] = []
    for v, h in zip(values, our_hexes):
        hws = _fmt_halfwords(h)
        asts = v.halfword_asterisks or tuple([False] * len(hws))
        for i, hw in enumerate(hws):
            pairs.append((_ast_slot(asts[i]), hw))
    HALFWORDS_PER_LINE = 16
    out = []
    for chunk_start in range(0, len(pairs), HALFWORDS_PER_LINE):
        chunk = pairs[chunk_start:chunk_start + HALFWORDS_PER_LINE]
        # 3-space indent, then alternating <ast><hex> separated by 1 space.
        body = "   " + " ".join(a + h for a, h in chunk)
        out.append(body + " " * max(0, LINE_WIDTH - len(body)))
    return out


def _emit_array_decimal_lines(our_hexes: list[str], values: list[FPValue], drv) -> list[str]:
    """Emit continuation decimal lines.  Input format:
    SP -> 5 cells/line, each cell = sign-slot + 13-char value (14 wide).
    DP -> 3 cells/line, each cell = sign-slot + 22-char value (23 wide).
    Cells are separated by 2 spaces; line indent is 4 spaces."""
    if not values:
        return []
    decs = [_our_decimal_for(v, h, drv) for v, h in zip(values, our_hexes)]
    type_class = values[0].type_class
    if type_class == "SP":
        per_line = 5
        cell_w = 14
    else:
        per_line = 3
        cell_w = 23
    out = []
    for chunk_start in range(0, len(decs), per_line):
        chunk = decs[chunk_start:chunk_start + per_line]
        body = "    " + "  ".join(f"{c:<{cell_w}}" for c in chunk)
        out.append(body + " " * max(0, LINE_WIDTH - len(body)))
    return out


def main():
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT

    sidecar = json.loads(SIDECAR_PATH.read_text())
    var_map = {k: v for k, v in sidecar.items() if not k.startswith("_")}

    src_lines = INPUT_PATH.read_text().splitlines(keepends=False)
    values = parse_lines([ln + "\n" for ln in src_lines])

    # Group FPValues by header line number.
    entries: dict[int, list[FPValue]] = defaultdict(list)
    for v in values:
        entries[v.line_number].append(v)

    # Determine each entry's continuation-line range by re-walking the
    # input file mirroring parse_lines's lookahead.  We track the set
    # of line indices that are "consumed" by some entry so we don't
    # emit them verbatim — we'll replace them with our regenerated
    # continuation lines.
    consumed: set[int] = set()    # 1-based input line numbers
    for header_ln in sorted(entries):
        # Walk forward while continuation predicate holds.
        j = header_ln + 1
        while j <= len(src_lines):
            cline = src_lines[j - 1]
            stripped = cline.strip()
            if not stripped:
                j += 1
                continue
            if not cline[0].isspace():
                break
            if stripped.startswith("["):
                break
            tokens = stripped.split()
            is_hex = all(re.fullmatch(r'\*?[0-9A-Fa-f]{4}', t) for t in tokens)
            is_dec = all(_DECIMAL_TOKEN.match(t) for t in tokens)
            if not (is_hex or is_dec):
                break
            consumed.add(j)
            j += 1

    out: list[str] = []
    with HalfpDriver(str(DRIVER_PATH)) as drv:
        wrap = _D(drv)
        for idx, line in enumerate(src_lines, start=1):
            if idx in entries:
                # Re-emit header with our values.
                vals = entries[idx]
                # Layout (from byte-level inspection of the input):
                #   cols 0..31  name + padding   (32 wide)
                #   cols 32..56 hex zone         (25 wide; col 32 is the
                #                                 asterisk slot for hw1)
                #   cols 57..80 decimal zone     (24 wide)
                #   cols 81..   type triplet + literals
                prefix = line[:32] if len(line) >= 32 else line.ljust(32)
                tail = line[81:] if len(line) >= 81 else ""

                # Tail might also start mid-string for short lines.  If
                # the original entry had no hex/decimal cols (e.g.,
                # ARRAY header), tail should still pick up at col 79 —
                # use the rest of the line if we're past 79.
                # Compute OUR hex once per value; both the hex zone and
                # the decimal zone consume the same value so the file is
                # internally self-consistent.
                our_hexes = [_our_hex_for(v, drv, wrap, var_map) for v in vals]
                hex_zone = _emit_header_hex_zone(our_hexes, vals)
                dec_zone = _emit_header_decimal_zone(our_hexes, vals, drv)
                out.append(prefix + hex_zone + dec_zone + tail)

                # Array entries: emit continuation hex + decimal lines.
                if vals[0].aggregate_kind == "ARRAY" or len(vals) > 1:
                    out.extend(_emit_array_hex_lines(our_hexes, vals))
                    out.extend(_emit_array_decimal_lines(our_hexes, vals, drv))

            elif idx in consumed:
                # This was a continuation line we replaced above. Skip.
                continue
            else:
                # Section header, blank line, unparseable — pass through.
                out.append(line)

    out_path.write_text("\n".join(out) + "\n")
    print(f"Wrote {out_path}")
    print(f"  Input lines:        {len(src_lines)}")
    print(f"  Output lines:       {len(out)}")
    print(f"  Entries reformatted:{len(entries)}")
    print(f"  Continuation lines replaced: {len(consumed)}")


if __name__ == "__main__":
    main()
