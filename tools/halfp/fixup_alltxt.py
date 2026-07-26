"""
Fix the "displaced close-paren" malformation in
testCases/vagc#1296/all.txt.

The pattern: the upstream extractor occasionally collapsed multiple
sequential variable declarations onto a single line, with this exact
shape:

  NAME `firstvalue)` `VAR2 TYPE INITIAL(value)` `VAR3 TYPE ... INITIAL(value)` ... `LAST TYPE INITIAL(value`

— note the FIRST backtick-quoted value has a spurious trailing `)` and
the LAST value is missing its closing `)`.  The misplaced paren belongs
at the end of the last declaration; the rest are clean
`VAR TYPE INITIAL(value)` strings inside backticks.

The fix:
  * Strip the trailing `)` from the first value.
  * Append `)` to the last value (so its INITIAL(...) balances).
  * Re-emit:
      NAME `fixed-first-value`         (parser-friendly format, line 1)
      VAR2 TYPE INITIAL(value)         (full declaration, no backticks)
      VAR3 TYPE INITIAL(value)
      ...
      LAST TYPE INITIAL(value)

Detection: first backtick-quoted value contains more `)` than `(`,
and there are 2+ backtick-quoted values on the line.

We only handle this exact shape; lines with other malformations
(unclosed `$(` from array indexing, etc.) are left alone.

Run from repo root.  Modifies all.txt in place.
    PYTHONPATH=. python3 tools/halfp/fixup_alltxt.py
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLTXT_PATH = REPO_ROOT / "testCases/vagc#1296/all.txt"

LINE_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\s+((?:`[^`]*`(?:\s+|$))+)\s*$')
VALUE_RE = re.compile(r'`([^`]*)`')


def looks_malformed(values: list[str]) -> bool:
    """Pattern-A detection: 2+ values, first has spurious trailing `)`,
    and at least one subsequent value contains `INITIAL(` (indicating it
    really IS a full variable declaration, not a bare name).

    Pattern-B (bare-name-with-shared-type) lines also have an unbalanced
    first paren but their subsequent values are bare identifiers; we
    skip those — without the original variable name we can't safely
    reconstruct full declarations."""
    if len(values) < 2:
        return False
    first = values[0]
    if first.count(")") <= first.count("("):
        return False
    return any("INITIAL(" in v for v in values[1:])


def fix_entry(values: list[str]) -> tuple[str, list[str]]:
    """Apply the displaced-paren fix.  Returns (first_value_fixed,
    list_of_subsequent_full_declarations)."""
    first = values[0]
    rest  = values[1:]

    # Strip exactly one trailing ')' from the first value.
    if first.endswith(")"):
        first_fixed = first[:-1]
    else:
        # Defensive — count says there's an extra ')' but it's not at
        # the very end.  Drop the last unmatched ')' we can find.
        # Simplest: drop the last ')' character.
        idx = first.rfind(")")
        first_fixed = first[:idx] + first[idx+1:] if idx >= 0 else first

    # Append ')' to the LAST subsequent declaration if unbalanced.
    if rest:
        last = rest[-1]
        if last.count("(") > last.count(")"):
            rest[-1] = last + ")"

    return first_fixed, rest


def main():
    src = ALLTXT_PATH.read_text().splitlines(keepends=False)
    out: list[str] = []
    fixed_count = 0
    fixes_log: list[tuple[int, str]] = []

    for ln_no, line in enumerate(src, start=1):
        m = LINE_RE.match(line)
        if not m:
            out.append(line)
            continue
        name = m.group(1)
        values = VALUE_RE.findall(m.group(2))
        if not looks_malformed(values):
            out.append(line)
            continue

        first_fixed, rest = fix_entry(values)
        # Emit the fixed first line in the original parser-friendly shape:
        out.append(f"{name} `{first_fixed}`")
        # Emit each subsequent declaration on its own line (no backticks).
        for d in rest:
            out.append(d)
        fixed_count += 1
        fixes_log.append((ln_no, name))

    ALLTXT_PATH.write_text("\n".join(out) + "\n")

    print(f"Fixed {fixed_count} malformed entries:")
    for ln_no, name in fixes_log:
        print(f"  line {ln_no:>5}: {name}")
    print()
    print(f"Input lines:  {len(src)}")
    print(f"Output lines: {len(out)}")
    print(f"Wrote {ALLTXT_PATH}")


if __name__ == "__main__":
    main()
