#!/usr/bin/env python3
#
# Compare two FCM images section-by-section.
#

import json
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer

from .addr import Addr, AddrDisp

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
        addr_to_rld[hw_addr] = f"RLD {sym} -> {target.x}"

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
    sections.sort(key=lambda t: t[1])
    return sections


def compare(sections, image_a, image_b, max_hw_diffs, addr_to_sym, addr_to_rld,
            equiv=None):
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

            shown = min(len(diff_positions), max_hw_diffs)
            pad = " " * 8 + " " * name_width
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

            failures += 1

    return checked, failures


@app.command()
def main(
    sym_json: Annotated[
        Path, typer.Argument(help="Symbol table JSON from linker", exists=True)
    ],
    fcm_a: Annotated[Path, typer.Argument(help="First FCM image", exists=True)],
    fcm_b: Annotated[Path, typer.Argument(help="Second FCM image", exists=True)],
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
    equiv: Annotated[
        str,
        typer.Option(
            "--equiv",
            help="Comma-separated hex halfwords treated as equivalent "
                 "(e.g. 0000,C6C6,C9FB). Pass '' or a single value to disable.",
        ),
    ] = "0000,C6C6,C9FB",
):
    """Compare two FCM images section-by-section."""
    modules = set(module) if module else None

    # Parse equiv set
    equiv_set = None
    if equiv:
        vals = [int(v, 16) for v in equiv.split(",") if v.strip()]
        if len(vals) >= 2:
            equiv_set = frozenset(vals)

    sections = load_sections(sym_json, modules=modules, include_all=all)
    addr_to_sym, addr_to_rld = load_annotations(sym_json, csect_table)

    image_a = fcm_a.read_bytes()
    image_b = fcm_b.read_bytes()

    if len(image_a) != len(image_b):
        print(
            f"Note: images differ in size "
            f"({len(image_a) // 2} vs {len(image_b) // 2} halfwords)"
        )

    checked, failures = compare(
        sections, image_a, image_b, max_hw_diffs, addr_to_sym, addr_to_rld,
        equiv=equiv_set,
    )

    if failures:
        print(f"\nFAIL: {failures}/{checked} section(s) differ")
        raise typer.Exit(1)
    else:
        print(f"\nPASS: all {checked} sections match")


if __name__ == "__main__":
    app()
