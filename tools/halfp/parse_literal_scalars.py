"""
Parser for the literalSCALARS.txt format produced by rburkey's MAFGEN
extraction (virtualagc issue #1296).

Each "row" describes a HAL/S declared variable that is initialized in the
compiled binary image. A row may be:

  * A scalar (single value):
        AIBK_DELTA  3F12  73D5  BAB2  1815  4.5049999999999990E-03  DP SCALAR  VARIABLE 0.004505

  * A 3-element vector or N-element array, with the variable name and the
    type triple (DP/SP, SCALAR/VECTOR, VARIABLE/TERMINAL) on one header line
    and the hex / decimal element listings on subsequent continuation lines:
        DELR    DP VECTOR  VARIABLE
            C6C6  C6C6  C6C6  C6C6  C6C6  C6C6  C6C6  C6C6  C6C6  C6C6  C6C6  C6C6
            -1.3027014776470587E+07  -1.3027014776470587E+07  -1.3027014776470587E+07

  * Empty (a declaration with no init values present).

Hex groups may be prefixed with `*`, which marks values that were hand-patched
in the binary at IBM's bench (rburkey's own annotation). We track this
per-element since forward conversion of a hand-patched value is meaningless.

Each value is materialized as one FPValue record. All strings are preserved
exactly as they appear in the file - no float() roundtrips.
"""

from dataclasses import dataclass
from typing import Optional
import re

# Regex for hex tokens: optional '*' prefix, then 4 hex digits.
_HEX_TOKEN = re.compile(r'(\*?)([0-9A-Fa-f]{4})')

# Regex for decimal tokens (MAFGEN-style or source-literal floats).
_DECIMAL_TOKEN = re.compile(
    r'^[+-]?(?:\d+\.?\d*|\.\d+)(?:[Ee][+-]?\d+)?$'
)

# Header anchor: "DP SCALAR VARIABLE" / "SP VECTOR TERMINAL" / etc.
_TYPE_TRIPLET = re.compile(
    r'\b(SP|DP)\s+(SCALAR|VECTOR)\s+(VARIABLE|TERMINAL)\b'
)


@dataclass
class FPValue:
    """One scalar value extracted from the literalSCALARS file.

    All string fields preserve the file's bytes exactly (no normalization).
    """
    file_section: str           # e.g. "DASS_G3.ASC"
    line_number: int            # 1-based line where this value's row starts
    name: str                   # HAL/S identifier
    aggregate_kind: Optional[str]   # None for scalars, "ARRAY" for arrays
    aggregate_index: int        # 0-based; 0 for scalars
    aggregate_size: int         # element count of the parent declaration
    type_class: str             # "SP" or "DP"
    storage_class: str          # "SCALAR" or "VECTOR"
    access_class: str           # "VARIABLE" or "TERMINAL"
    hex: str                    # canonical 8-char (SP) or 16-char (DP) uppercase
    asterisked: bool            # any of this value's groups had '*' prefix
    halfword_asterisks: tuple   # per-halfword bool (2 entries SP, 4 entries DP)
    mafgen_decimal: Optional[str]   # exact string from file
    source_literal: Optional[str]   # exact source-text decimal, or None


def parse_file(path):
    """Parse a literalSCALARS-format file. Returns list[FPValue]."""
    with open(path) as f:
        lines = f.readlines()
    return parse_lines(lines)


def parse_lines(lines):
    values: list[FPValue] = []
    section = ""
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\n")
        if not line.strip():
            i += 1
            continue
        if line.lstrip().startswith("["):
            # File-section header, e.g. "[DASS_G3.ASC]"
            section = line.strip().strip("[]")
            i += 1
            continue
        if line[0].isspace():
            # Stray continuation with no header. Skip.
            i += 1
            continue

        # Header line. Parse it and look ahead for continuations.
        triplet = _TYPE_TRIPLET.search(line)
        if not triplet:
            # Unknown line shape; skip.
            i += 1
            continue

        type_class = triplet.group(1)
        storage_class = triplet.group(2)
        access_class = triplet.group(3)

        name = line[:32].rstrip()

        # Hex zone occupies cols 32..54. Each 4-digit group's leading column
        # is either a space or '*' (hand-patched marker).
        hex_zone = line[32:55] if len(line) >= 32 else ""
        header_hex = _HEX_TOKEN.findall(hex_zone)

        # MAFGEN decimal occupies cols 56..78 (23 chars wide). Negative values
        # place '-' at col 56; positive values pad with a space there. Validate
        # the strip()'d result as a decimal so we don't pick up "ARRAY".
        mafgen_zone = line[56:79].strip() if len(line) >= 56 else ""
        header_decimal = mafgen_zone if _DECIMAL_TOKEN.match(mafgen_zone) else None

        # Detect ARRAY keyword. The keyword "VECTOR" can also appear, but only
        # as the storage_class; the aggregate marker is exclusively "ARRAY".
        rest_zone = line[33:triplet.start()]
        agg_kind = "ARRAY" if re.search(r'\bARRAY\b', rest_zone) else None

        # Source literals: everything after the type triplet.
        # Updated format: each per-element initial is wrapped in '...' single
        # quotes; the quoted content can be a numeric literal, a bare variable
        # name, or an expression. Older un-quoted tokens are still tolerated
        # (whitespace-split) so we can parse historical files too.
        after_triplet = line[triplet.end():].strip()
        if after_triplet:
            quoted = re.findall(r"'([^']*)'", after_triplet)
            if quoted:
                source_literals = quoted
            else:
                source_literals = after_triplet.split()
        else:
            source_literals = []

        # Look ahead for continuation lines (leading whitespace, no section
        # header, recognizable hex-only or decimal-only content).
        cont_hex: list[tuple[str, str]] = []
        cont_dec: list[str] = []
        j = i + 1
        while j < len(lines):
            cline = lines[j].rstrip("\n")
            if not cline.strip():
                j += 1
                continue
            if not cline[0].isspace():
                break
            if cline.lstrip().startswith("["):
                break
            tokens = cline.split()
            if all(re.fullmatch(r'\*?[0-9A-Fa-f]{4}', t) for t in tokens):
                cont_hex.extend(_HEX_TOKEN.findall(cline))
            elif all(_DECIMAL_TOKEN.match(t) for t in tokens):
                cont_dec.extend(tokens)
            else:
                # Unrecognized continuation; ignore but don't break (defensive).
                pass
            j += 1

        all_hex = header_hex + cont_hex
        all_dec = ([header_decimal] if header_decimal else []) + cont_dec

        groups_per_value = 2 if type_class == "SP" else 4
        if all_hex and len(all_hex) % groups_per_value == 0:
            nvalues = len(all_hex) // groups_per_value
        else:
            nvalues = 0

        # Only pair source literals to elements when the count matches
        # exactly. Mismatches (e.g. a TERMINAL scalar listing 9 unrelated
        # decimals after the type triple) would otherwise produce false
        # forward-conversion failures.
        literals_match = (len(source_literals) == nvalues)

        for vidx in range(nvalues):
            grp = all_hex[vidx * groups_per_value:(vidx + 1) * groups_per_value]
            asterisked = any(g[0] == '*' for g in grp)
            halfword_asterisks = tuple(g[0] == '*' for g in grp)
            hex_str = "".join(g[1].upper() for g in grp)
            mafgen_dec = all_dec[vidx] if vidx < len(all_dec) else None
            src_lit = source_literals[vidx] if literals_match else None
            # '@' is rburkey's placeholder (in the older pre-expression
            # extraction) for "this slot's INITIAL was an expression, not a
            # literal". The newer file embeds expressions in '...' quotes
            # directly, but a bare '' (empty quote pair) still means "no
            # usable initial".
            if src_lit == "@" or src_lit == "":
                src_lit = None
            values.append(FPValue(
                file_section=section,
                line_number=i + 1,
                name=name,
                aggregate_kind=agg_kind,
                aggregate_index=vidx,
                aggregate_size=nvalues,
                type_class=type_class,
                storage_class=storage_class,
                access_class=access_class,
                hex=hex_str,
                asterisked=asterisked,
                halfword_asterisks=halfword_asterisks,
                mafgen_decimal=mafgen_dec,
                source_literal=src_lit,
            ))

        i = j

    return values


def summarize(values):
    """Print a human-readable summary of the parsed dataset."""
    from collections import Counter

    by_type = Counter(v.type_class for v in values)
    by_agg = Counter(v.aggregate_kind or "(scalar)" for v in values)
    by_storage = Counter(v.storage_class for v in values)
    by_access = Counter(v.access_class for v in values)
    asterisked = sum(1 for v in values if v.asterisked)
    has_lit = sum(1 for v in values if v.source_literal is not None)
    has_mafgen = sum(1 for v in values if v.mafgen_decimal is not None)

    print(f"Total values:          {len(values)}")
    print(f"  by type_class:       {dict(by_type)}")
    print(f"  by aggregate_kind:   {dict(by_agg)}")
    print(f"  by storage_class:    {dict(by_storage)}")
    print(f"  by access_class:     {dict(by_access)}")
    print(f"  asterisked:          {asterisked}")
    print(f"  has source literal:  {has_lit}")
    print(f"  has mafgen decimal:  {has_mafgen}")

    # Cross-tab: SP/DP × asterisked × has_literal
    print()
    print("Cross-tab (type x asterisked x has-literal):")
    cross = Counter()
    for v in values:
        cross[(v.type_class, v.asterisked, v.source_literal is not None)] += 1
    for (t, ast, lit), n in sorted(cross.items()):
        print(f"  {t}  asterisked={ast!s:5}  has_literal={lit!s:5}  -> {n}")

    sections = Counter(v.file_section for v in values)
    print()
    print(f"File sections ({len(sections)}):")
    for name, n in sections.most_common():
        print(f"  {name:20s}  {n}")


def main(argv):
    import argparse
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument("path", help="path to literalSCALARS.txt")
    p.add_argument("--show", type=int, default=0,
                   help="print first N records to stderr for spot-check")
    args = p.parse_args(argv)

    values = parse_file(args.path)
    summarize(values)

    if args.show:
        import sys
        print(f"\nFirst {args.show} records:", file=sys.stderr)
        for v in values[:args.show]:
            print(v, file=sys.stderr)


if __name__ == "__main__":
    import sys
    main(sys.argv[1:])
