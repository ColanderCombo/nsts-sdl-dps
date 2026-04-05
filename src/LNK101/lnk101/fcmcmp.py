#!/usr/bin/env python3
#
# Compare two FCM images section-by-section.
#

import json
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Annotated, Optional

import typer

from .addr import Addr, AddrDisp
from .repro import ReproTracker, version_string

def _version_callback(value: bool):
    if value:
        print(f"fcmcmp {version_string()}")
        raise typer.Exit()


app = typer.Typer(
    help="Compare two FCM images section-by-section using a symbol table.",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)

# data created by the linker, not from real .obj files
_SYNTHETIC_MODULES = {
    "<external-syms>",
    "<ext-syms>",
    "<generated-stacks>",
    "<stacks>",
    "<defined-symbols>",
    "<defined>",
}


def load_annotations(sym_json_path, csect_table_path=None):
    """Build lookup tables for annotating diff output.

    Returns:
        addr_to_sym: halfword address -> symbol name
        addr_to_rld: halfword address -> "RLD symbol -> target_addr"
    """
    addr_to_sym = {}
    addr_to_rld = {}

    with open(sym_json_path) as f:
        sym_data = json.load(f)

    for s in sym_data.get("symbols", []):
        addr_to_sym[s["address"]] = s["name"]

    for r in sym_data.get("relocations", []):
        hw_addr = r["address"]
        target = Addr.from_hw(r["target"])
        sym = r.get("symbol", "")
        targetName = r.get("targetName", "")
        flags = r.get("flags", 0)
        label = f"{targetName} ({sym})" if targetName and sym and targetName != sym else (sym or targetName)
        neg = " (negative disp.)" if flags & 0x80 else ""
        addr_to_rld[hw_addr] = f"RLD {label} -> {target.x}{neg}"

    if csect_table_path:
        with open(csect_table_path) as f:
            csect_data = json.load(f)
        for name, info in csect_data.items():
            start = info.get("start")
            if start is not None:
                addr_to_sym.setdefault(start, name)

    return addr_to_sym, addr_to_rld


def load_sections(sym_json_path, modules=None, include_all=False):
    with open(sym_json_path) as f:
        data = json.load(f)

    if modules is None and not include_all:
        modules = set()
        for m in data.get("modules", []):
            fn = m.get("filename", "")
            if not fn.startswith("<"):
                modules.add(m["name"])

    sections = []
    for s in data["sections"]:
        if modules and s["module"] not in modules:
            continue
        addr = Addr.from_hw(s["address"])
        size = AddrDisp.from_hw(s["size"])
        sections.append((s["name"], addr, size))
    return sections


# All known 2-character CSECT name prefixes.
_CSECT_PREFIXES = (
    # from USA-003089/p.106 sect.3.2 Object Code Naming Conventions:
    {'#C', '#D', '#P', '#E', '#Z', '#R', '#X', '#Q', '#L'}
    | {f'${c}' for c in '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'} # $0 PROGRAM $[1-9a-z] TASK
    | {f'@{n}' for n in '0123456789'}
    | {f'{a}{n}' for a in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' for n in '0123456789'} # Internal proc
)


def section_base_name(name):
    return name[2:] if (len(name) >= 2 and name[:2] in _CSECT_PREFIXES) else name


def _read_hw(buf, byte_off):
    return (buf[byte_off] << 8) | buf[byte_off + 1]


def detect_shift(a, b, first_diff_hw):
    """Check whether a block of diffs looks like inserted/deleted halfwords.

    Compares halfwords from *first_diff_hw* to end-of-section, trying
    small shifts of ``a`` relative to ``b``.

    Returns the best shift (int) or ``None``.
    shift > 0 means the first image has extra halfwords (insertion);
    shift < 0 means the first image is missing halfwords (deletion).
    """
    scan_start = first_diff_hw  # halfword index within section
    scan_hw = min(len(a), len(b)) // 2 - scan_start
    if scan_hw < 6:
        return None

    a_hw = len(a) // 2
    b_hw = len(b) // 2

    def count_matches(shift):
        matches = 0
        compared = 0
        for hi in range(scan_start, b_hw):
            si = hi + shift
            if 0 <= si < a_hw:
                if _read_hw(a, si * 2) == _read_hw(b, hi * 2):
                    matches += 1
                compared += 1
        return matches, compared

    no_shift, _ = count_matches(0)

    best_shift = 0
    best_matches = no_shift
    for s in range(-8, 9):
        if s == 0:
            continue
        m, c = count_matches(s)
        if c > 0 and m > best_matches:
            best_matches = m
            best_shift = s

    if best_shift == 0:
        return None
    improvement = best_matches - no_shift
    if improvement < scan_hw * 0.2 or best_matches < scan_hw * 0.3:
        return None
    return best_shift


def sort_sections(sections, group_by_name=False):
    if group_by_name:
        sections.sort(key=lambda t: (section_base_name(t[0]), t[1]))
    else:
        sections.sort(key=lambda t: t[1])
    return sections


def _print_diffs(diff_positions, max_hw_diffs, pad, addr_to_sym, addr_to_rld):
    """Print individual halfword diffs with annotations."""
    shown = min(len(diff_positions), max_hw_diffs)
    for diff_addr, hw_a, hw_b in diff_positions[:shown]:
        notes = []
        sym = addr_to_sym.get(diff_addr.hw)
        if sym:
            notes.append(sym)
        rld = addr_to_rld.get(diff_addr.hw)
        if rld:
            notes.append(rld)
        annotation = f"  ; {', '.join(notes)}" if notes else ""
        print(
            f"{pad} @ {diff_addr.x}"
            f" {hw_a:04X} vs {hw_b:04X}{annotation}"
        )
    remaining = len(diff_positions) - shown
    if remaining > 0:
        print(f"{pad}   ... and {remaining} more")


def compare(sections, image_a, image_b, max_hw_diffs, addr_to_sym, addr_to_rld,
            equiv=None, diff_if_shifted=True):
    failures = 0
    checked = 0

    # Compute column width for aligned '@'
    name_width = max((len(name) for name, _, _ in sections), default=0)

    for name, addr, size in sections:
        offset = addr.bytes
        length = size.bytes
        padded = name.ljust(name_width)

        if offset + length > len(image_a):
            print(
                f"  SKIP: {padded} @ {addr.x} ({size.hw} halfwords)"
                f" — beyond end of first image"
            )
            continue
        if offset + length > len(image_b):
            print(
                f"  SKIP: {padded} @ {addr.x} ({size.hw} halfwords)"
                f" — beyond end of second image"
            )
            continue

        a = image_a[offset : offset + length]
        b = image_b[offset : offset + length]
        checked += 1

        diff_positions = []
        if a != b:
            for hi in range(size.hw):
                bo = hi * 2
                hw_a = (a[bo] << 8) | a[bo + 1] if bo + 1 < len(a) else a[bo] << 8
                hw_b = (b[bo] << 8) | b[bo + 1] if bo + 1 < len(b) else b[bo] << 8
                if hw_a != hw_b:
                    if equiv and hw_a in equiv and hw_b in equiv:
                        continue
                    diff_positions.append((addr + Addr(hi * 2), hw_a, hw_b))

        if not diff_positions:
            print(f"  OK:   {padded} @ {addr.x} ({size.hw} halfwords)")
        else:
            print(
                f"  FAIL: {padded} @ {addr.x} ({size.hw} halfwords)"
                f" — {len(diff_positions)} halfwords differ"
            )

            pad = " " * 8 + " " * name_width
            _print_diffs(diff_positions, max_hw_diffs, pad, addr_to_sym,
                         addr_to_rld)

            # Check for shifted code (inserted/deleted halfwords)
            if diff_if_shifted:
                first_diff_hw = (diff_positions[0][0] - addr).hw
                shift = detect_shift(a, b, first_diff_hw)
                if shift:
                    shift_addr = diff_positions[0][0]
                    if shift > 0:
                        desc = (f"{shift} halfword(s) inserted in first image"
                                f" near {shift_addr.x}")
                        # Show the inserted halfwords
                        inserted = []
                        for i in range(shift):
                            hi = first_diff_hw + i
                            if hi * 2 + 1 < len(a):
                                inserted.append(f"{_read_hw(a, hi * 2):04X}")
                        print(f"{pad}   ** shift {shift:+d}: {desc}")
                        print(f"{pad}      inserted: {' '.join(inserted)}")
                    else:
                        desc = (f"{-shift} halfword(s) deleted from first image"
                                f" near {shift_addr.x}")
                        # Show the deleted halfwords (present in b but not a)
                        deleted = []
                        for i in range(-shift):
                            hi = first_diff_hw + i
                            if hi * 2 + 1 < len(b):
                                deleted.append(f"{_read_hw(b, hi * 2):04X}")
                        print(f"{pad}   ** shift {shift:+d}: {desc}")
                        print(f"{pad}      deleted: {' '.join(deleted)}")

                    # Re-compare with shift applied, show remaining diffs
                    shifted_diffs = []
                    a_hw = len(a) // 2
                    b_hw = len(b) // 2
                    for hi in range(first_diff_hw, b_hw):
                        si = hi + shift
                        if 0 <= si < a_hw:
                            va = _read_hw(a, si * 2)
                            vb = _read_hw(b, hi * 2)
                            if va != vb:
                                if equiv and va in equiv and vb in equiv:
                                    continue
                                shifted_diffs.append(
                                    (addr + Addr(hi * 2), va, vb))
                    if shifted_diffs:
                        print(f"{pad}      after shift,"
                              f" {len(shifted_diffs)} halfwords"
                              f" still differ:")
                        _print_diffs(shifted_diffs, max_hw_diffs,
                                     pad + "      ", addr_to_sym,
                                     addr_to_rld)
                    else:
                        print(f"{pad}      after shift, all halfwords match")

            failures += 1

    return checked, failures


def collect_diffs(sections, image_a, image_b, equiv=None):
    """Return all halfword differences across all sections (no elision).

    Returns a list of dicts with keys: section, address, hw_a, hw_b.
    Addresses are 5-char hex, values are 4-char hex (uppercase).
    """
    diffs = []
    for name, addr, size in sections:
        offset = addr.bytes
        length = size.bytes
        if offset + length > len(image_a) or offset + length > len(image_b):
            continue
        a = image_a[offset : offset + length]
        b = image_b[offset : offset + length]
        if a == b:
            continue
        for hi in range(size.hw):
            bo = hi * 2
            hw_a = (a[bo] << 8) | a[bo + 1] if bo + 1 < len(a) else a[bo] << 8
            hw_b = (b[bo] << 8) | b[bo + 1] if bo + 1 < len(b) else b[bo] << 8
            if hw_a != hw_b:
                if equiv and hw_a in equiv and hw_b in equiv:
                    continue
                diff_addr = addr + Addr(hi * 2)
                diffs.append({
                    "section": name,
                    "address": f"{diff_addr.hw:05X}",
                    "hw_a": f"{hw_a:04X}",
                    "hw_b": f"{hw_b:04X}",
                })
    return diffs


def compare_with_diff_json(diff_data, image, max_hw_diffs, addr_to_sym,
                           addr_to_rld):
    """Compare an FCM image against reference values from a diff JSON.

    For each recorded diff, reads the halfword at that address from *image*
    and checks whether it now matches the reference value (hw_b).

    Returns (total_known, total_fixed, total_remaining).
    """
    entries = diff_data["diffs"]

    # Group by section, preserving order of first appearance
    by_section = OrderedDict()
    for d in entries:
        by_section.setdefault(d["section"], []).append(d)

    total_known = len(entries)
    total_fixed = 0
    total_remaining = 0

    name_width = max((len(name) for name in by_section), default=0)

    for section_name, section_diffs in by_section.items():
        fixed = 0
        remaining = []

        for d in section_diffs:
            hw_addr = int(d["address"], 16)
            byte_offset = hw_addr * 2

            if byte_offset + 1 < len(image):
                our_val = (image[byte_offset] << 8) | image[byte_offset + 1]
            elif byte_offset < len(image):
                our_val = image[byte_offset] << 8
            else:
                our_val = None

            ref_val = int(d["hw_b"], 16)

            if our_val == ref_val:
                fixed += 1
            else:
                remaining.append((d["address"], our_val, ref_val, d["hw_a"]))

        padded = section_name.ljust(name_width)
        total_fixed += fixed
        total_remaining += len(remaining)

        if not remaining:
            print(
                f"  OK:   {padded}"
                f" — all {len(section_diffs)} known diffs now match reference"
            )
        else:
            print(
                f"  DIFF: {padded}"
                f" — {len(remaining)}/{len(section_diffs)} diffs remain"
                f" ({fixed} fixed)"
            )
            pad = " " * 8 + " " * name_width
            shown = min(len(remaining), max_hw_diffs)
            for addr_str, our_val, ref_val, orig_val in remaining[:shown]:
                notes = []
                hw = int(addr_str, 16)
                sym = addr_to_sym.get(hw)
                if sym:
                    notes.append(sym)
                rld = addr_to_rld.get(hw)
                if rld:
                    notes.append(rld)
                annotation = f"  ; {', '.join(notes)}" if notes else ""
                our_str = f"{our_val:04X}" if our_val is not None else "????"
                print(
                    f"{pad} @ {addr_str}"
                    f" {our_str} vs ref {ref_val:04X}"
                    f" (was {orig_val}){annotation}"
                )
            leftover = len(remaining) - shown
            if leftover > 0:
                print(f"{pad}   ... and {leftover} more")

    return total_known, total_fixed, total_remaining


@app.command()
def main(
    sym_json: Annotated[
        Path, typer.Argument(help="Symbol table JSON from linker", exists=True)
    ],
    fcm_a: Annotated[Path, typer.Argument(help="First FCM image", exists=True)],
    fcm_b: Annotated[
        Optional[Path], typer.Argument(help="Second FCM image")
    ] = None,
    module: Annotated[
        Optional[list[str]],
        typer.Option(
            "--module", "-m", help="Only compare sections from this module (repeatable)"
        ),
    ] = None,
    all: Annotated[
        bool,
        typer.Option(
            "--all", help="Include all sections (including synthetic/generated)"
        ),
    ] = False,
    max_hw_diffs: Annotated[
        int,
        typer.Option(
            "--max-hw-diffs", help="Max differing halfwords to show per section"
        ),
    ] = 32,
    csect_table: Annotated[
        Optional[Path],
        typer.Option(
            "--csect-table",
            help="csectTable.json for additional symbol annotations",
            exists=True,
        ),
    ] = None,
    group_by_name: Annotated[
        bool,
        typer.Option(
            "--group-sections-by-name",
            help="Group sections by base program name instead of sorting by address",
        ),
    ] = False,
    equiv: Annotated[
        str,
        typer.Option(
            "--equiv",
            help="Comma-separated hex halfwords treated as equivalent "
                 "(e.g. 0000,C6C6,C9FB). Pass '' or a single value to disable.",
        ),
    ] = "0000,C6C6,C9FB",
    diff_json: Annotated[
        Optional[Path],
        typer.Option(
            "--diff-json",
            help="Compare FCM_A against reference values from a diff JSON "
                 "(replaces FCM_B).",
            exists=True,
        ),
    ] = None,
    dump_diffs: Annotated[
        Optional[Path],
        typer.Option(
            "--dump-diffs",
            help="Dump all halfword diffs (no elision) to a JSON file.",
        ),
    ] = None,
    diff_if_shifted: Annotated[
        bool,
        typer.Option(
            "--diff-if-shifted/--no-diff-if-shifted",
            help="When a shift is detected, re-compare with shift applied.",
        ),
    ] = True,
    repro: Annotated[
        bool,
        typer.Option(
            "--repro/--no-repro",
            help="Print repro info (git version, file MD5s) and save .repro.json",
        ),
    ] = True,
    check_repro: Annotated[
        Optional[Path],
        typer.Option(
            "--check-repro",
            help="Compare current run against a saved .repro.json",
            exists=True,
        ),
    ] = None,
    version: Annotated[
        bool, typer.Option("--version", help="Show version",
                           callback=_version_callback, is_eager=True)
    ] = False,
):
    """Compare two FCM images section-by-section."""

    if diff_json and fcm_b:
        print("Error: --diff-json and FCM_B are mutually exclusive.", file=sys.stderr)
        raise typer.Exit(2)
    if not diff_json and not fcm_b:
        print("Error: provide either FCM_B or --diff-json.", file=sys.stderr)
        raise typer.Exit(2)
    if fcm_b and not fcm_b.exists():
        print(f"Error: {fcm_b} does not exist.", file=sys.stderr)
        raise typer.Exit(2)
    if dump_diffs and diff_json:
        print("Error: --dump-diffs requires FCM_B, not --diff-json.",
              file=sys.stderr)
        raise typer.Exit(2)

    modules = set(module) if module else None

    tracker = ReproTracker("fcmcmp")
    tracker.track(sym_json, role="sym_json")
    tracker.track(fcm_a, role="fcm_a")
    if fcm_b:
        tracker.track(fcm_b, role="fcm_b")
    if csect_table:
        tracker.track(csect_table, role="csect_table")
    if diff_json:
        tracker.track(diff_json, role="diff_json")

    # Parse equiv set
    equiv_set = None
    if equiv:
        vals = [int(v, 16) for v in equiv.split(",") if v.strip()]
        if len(vals) >= 2:
            equiv_set = frozenset(vals)

    addr_to_sym, addr_to_rld = load_annotations(sym_json, csect_table)

    # --diff-json mode: compare FCM_A against recorded reference values
    if diff_json:
        with open(diff_json) as f:
            diff_data = json.load(f)

        image_a = fcm_a.read_bytes()
        total_known, total_fixed, total_remaining = compare_with_diff_json(
            diff_data, image_a, max_hw_diffs, addr_to_sym, addr_to_rld,
        )

        if repro:
            tracker.print_summary()
            repro_path = Path(fcm_a.stem + '.fcmcmp.repro.json')
            tracker.save(repro_path, extra={"diffs": diff_data.get("diffs", [])})
        if check_repro:
            tracker.print_check(check_repro)

        print()
        if total_remaining == 0:
            print(f"All {total_known} known diffs now match reference")
        else:
            print(
                f"{total_fixed}/{total_known} known diffs fixed,"
                f" {total_remaining} remaining"
            )
            raise typer.Exit(1)
        return

    # Normal two-image comparison
    sections = load_sections(sym_json, modules=modules, include_all=all)
    sort_sections(sections, group_by_name=group_by_name)

    image_a = fcm_a.read_bytes()
    image_b = fcm_b.read_bytes()

    if len(image_a) != len(image_b):
        print(
            f"Note: images differ in size "
            f"({len(image_a) // 2} vs {len(image_b) // 2} halfwords)"
        )

    checked, failures = compare(
        sections, image_a, image_b, max_hw_diffs, addr_to_sym, addr_to_rld,
        equiv=equiv_set, diff_if_shifted=diff_if_shifted,
    )

    # Collect all diffs (no elision) when dumping or when repro needs them
    all_diffs = None
    if dump_diffs or repro:
        all_diffs = collect_diffs(sections, image_a, image_b, equiv=equiv_set)

    if dump_diffs:
        data = {"diffs": all_diffs, "repro": tracker.to_dict()}
        with open(dump_diffs, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        print(f"\nDumped {len(all_diffs)} diffs to {dump_diffs}")

    if repro:
        tracker.print_summary()
        repro_path = Path(fcm_a.stem + '.fcmcmp.repro.json')
        extra = {"diffs": all_diffs} if all_diffs is not None else {}
        tracker.save(repro_path, extra=extra)

    if check_repro:
        tracker.print_check(check_repro)

    if failures:
        print(f"\nFAIL: {failures}/{checked} section(s) differ")
        raise typer.Exit(1)
    else:
        print(f"\nPASS: all {checked} sections match")


if __name__ == "__main__":
    app()
