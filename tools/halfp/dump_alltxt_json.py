"""
Parse all of testCases/vagc#1296/all.txt — including multi-value (array)
declarations and special-typed values like HEX'...' / BIN'...' — and
emit the result as a JSON file at build/test/halfp/all.json.

Schema:
    {
      "NAME": {
        "values": ["raw_value_1", "raw_value_2", ...],
        "kind":   "numeric" | "hex" | "bin" | "char" | "true_false" |
                  "expression" | "mixed" | "unparsed"
      },
      ...
    }

  * "values" is the list of backtick-quoted contents in declaration order.
  * "kind" is a coarse classifier; consumers that want the parsed numeric
    DP hex should run dp_from_string on each "numeric" value.
  * Values that don't match any clean shape (the few malformed lines)
    are stored under "unparsed" with the raw line content.

Why this is useful: the previous diff_alltxt.py / apply_alltxt.py only
looked at single-value lines, ignoring 6,923 multi-value declarations.
Dumping everything lets downstream tools pick whichever subset they need
without re-parsing all.txt's quirks every time.

Run from repo root:
    PYTHONPATH=. python3 tools/halfp/dump_alltxt_json.py [output-path]
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLTXT_PATH = REPO_ROOT / "testCases/vagc#1296/all.txt"
DEFAULT_OUT = REPO_ROOT / "build/test/halfp/all.json"

# Capture: NAME followed by one or more backtick-quoted values.
LINE_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\s+((?:`[^`]*`(?:\s+|$))+)\s*$')
VALUE_RE = re.compile(r'`([^`]*)`')

# Full-declaration form (produced by fixup_alltxt.py when it splits a
# malformed multi-decl line into one decl per line):
#   NAME [type-info] INITIAL(value)
#   NAME [type-info] CONSTANT(value)
# We extract NAME and VALUE; the value can contain nested parens
# (e.g. 'BIN(12)'0''), so we count parens to find the matching ')' rather
# than using a non-nesting regex.
DECL_NAME_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\s+\S')
INITIAL_RE   = re.compile(r'\b(INITIAL|CONSTANT)\(')


def extract_declaration(line: str):
    """If line is `NAME ... INITIAL(value)` or `NAME ... CONSTANT(value)`,
    return (name, value).  Otherwise None.  Handles nested parens in
    the value (e.g. 'BIN(12)'0'')."""
    nm = DECL_NAME_RE.match(line)
    if not nm:
        return None
    name = nm.group(1)
    m = INITIAL_RE.search(line)
    if not m:
        return None
    start = m.end()      # just past the '('
    depth = 1
    i = start
    while i < len(line) and depth > 0:
        c = line[i]
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return (name, line[start:i])
        i += 1
    # Unmatched paren — return as much as we have so the value is at
    # least preserved for inspection.
    return (name, line[start:])

# Coarse kind classifiers.
NUMERIC_RE = re.compile(r'^[+\-]?(?:\d+\.?\d*|\.\d+)(?:[Ee][+\-]?\d+)?$')
HEX_RE     = re.compile(r"^HEX'[0-9A-Fa-f]+'$")
BIN_RE     = re.compile(r"^BIN'[01]+'$")
CHAR_RE    = re.compile(r"^'[^']*'$")
TRUEFALSE_RE = re.compile(r'^(TRUE|FALSE)$')
EXPR_RE    = re.compile(r'[+\-*/()A-Za-z_]')   # contains identifier/operator


def classify_value(v: str) -> str:
    if NUMERIC_RE.match(v):    return "numeric"
    if HEX_RE.match(v):        return "hex"
    if BIN_RE.match(v):        return "bin"
    if CHAR_RE.match(v):       return "char"
    if TRUEFALSE_RE.match(v):  return "true_false"
    if EXPR_RE.search(v):      return "expression"
    return "other"


def classify_values(values: list[str]) -> str:
    """Single kind that summarizes a list of values.  Mixed lists get
    'mixed'; otherwise the unique kind is returned."""
    kinds = {classify_value(v) for v in values}
    if len(kinds) == 1:
        return next(iter(kinds))
    return "mixed"


def main():
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    out_path.parent.mkdir(parents=True, exist_ok=True)

    out: dict = {}
    unparsed: list[str] = []
    duplicates: dict[str, int] = {}
    decl_count = 0

    with open(ALLTXT_PATH) as f:
        for ln_no, raw in enumerate(f, start=1):
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            # First try the backtick-quoted form (most lines).
            m = LINE_RE.match(line)
            if m:
                name = m.group(1)
                values = VALUE_RE.findall(m.group(2))
                entry = {
                    "values": values,
                    "kind":   classify_values(values),
                }
                if name in out:
                    duplicates[name] = duplicates.get(name, 1) + 1
                out[name] = entry
                continue
            # Fall back to the full-declaration form
            # (NAME [type] INITIAL(value) / CONSTANT(value)).  The fixup
            # script produces these when it splits malformed lines.
            decl = extract_declaration(line)
            if decl:
                name, value = decl
                entry = {
                    "values": [value],
                    "kind":   classify_values([value]),
                    "source": "declaration",
                }
                if name in out:
                    duplicates[name] = duplicates.get(name, 1) + 1
                out[name] = entry
                decl_count += 1
                continue
            unparsed.append(line)

    payload = {
        "_doc": (
            "Dump of testCases/vagc#1296/all.txt by tools/halfp/dump_alltxt_json.py. "
            "Each entry has 'values' (list of raw backtick-quoted strings, "
            "preserving the declaration order) and 'kind' (coarse classifier: "
            "numeric, hex, bin, char, true_false, expression, mixed, other)."
        ),
        "_unparsed_count": len(unparsed),
        "_duplicate_names": duplicates,
        "entries": out,
    }
    out_path.write_text(json.dumps(payload, indent=2) + "\n")

    # Print a compact summary.
    print(f"Wrote {out_path}")
    print(f"  Total entries:     {len(out)}")
    print(f"  Backtick form:     {len(out) - decl_count}")
    print(f"  Declaration form:  {decl_count}")
    print(f"  Unparsed lines:    {len(unparsed)}")
    if duplicates:
        print(f"  Duplicates seen:   {len(duplicates)} names "
              f"(latest declaration kept)")
    # Per-kind histogram
    from collections import Counter
    counts = Counter(e["kind"] for e in out.values())
    print(f"  By kind:")
    for k, n in counts.most_common():
        print(f"    {k:<12}  {n}")
    # Per-arity histogram (1 = single value, >1 = array)
    arity = Counter(len(e["values"]) for e in out.values())
    print(f"  By arity:")
    for k in sorted(arity):
        print(f"    {k:>3} value(s):  {arity[k]}")


if __name__ == "__main__":
    main()
