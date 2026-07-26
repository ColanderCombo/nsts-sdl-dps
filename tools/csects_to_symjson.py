#!/usr/bin/env python3
"""Convert a MAFGEN ``csects-*.json`` file into the GPC simulator's
``*.sym.json`` symbol-table format.

The MAFGEN disassembler emits a flat ``name -> csect`` map::

    {
      "ACOS": {
        "start": 291068,            # halfword address
        "end":   291183,            # halfword address (inclusive)
        "type":  "HAL_LIBRARY_CODE",
        "contents": { "ASIN": 8, "POLY": 42, ... }   # label -> halfword offset
      },
      ...
    }

The GPC simulator's SymbolTable (ext/sim/gpc/symbolTable.coffee), however,
expects the LNK101 linker schema::

    {
      "entryPoint": <hw>,
      "sections":  [ {"name","address","size","module"}, ... ],
      "symbols":   [ {"name","address","type","section","module"}, ... ],
      "relocations": [ ... ]
    }

All MAFGEN addresses (start/end and the per-label content offsets) are
halfword units, which is exactly the unit LNK101 uses (``.hw``), so the
conversion is a straight remap with no scaling.

Usage::

    csects_to_symjson.py csects-SSW.json [-o SSW.sym.json] [--entry NAME]

With no -o, writes alongside the input as ``<stem>.sym.json`` (mafgen
``csects-SSW.json`` -> ``SSW.sym.json`` so it sits next to ``SSW.fcm`` and
is also picked up by the simulator's auto-detect).
"""
import argparse
import json
import re
import sys
from pathlib import Path


def convert(csects: dict, entry_name: str | None = None) -> dict:
    sections = []
    symbols = []
    max_end = 0

    for name, info in csects.items():
        start = info["start"]
        end = info.get("end", start)
        max_end = max(max_end, end)
        size = max(1, end - start + 1)
        sec_type = info.get("type", "")

        sections.append({
            "name": name,
            "address": start,
            "size": size,
            "module": "mafgen",
            "mafgenType": sec_type,
        })

        # The csect itself becomes a "section" symbol at its base address.
        symbols.append({
            "name": name,
            "address": start,
            "type": "section",
            "section": None,
            "module": "mafgen",
        })

        # Each interior label becomes an "entry" symbol; offsets are
        # halfwords relative to the csect's start.
        for label, offset in (info.get("contents") or {}).items():
            symbols.append({
                "name": label,
                "address": start + offset,
                "type": "entry",
                "section": name,
                "module": "mafgen",
            })

    sections.sort(key=lambda s: s["address"])
    symbols.sort(key=lambda s: s["address"])

    entry_point = None
    if entry_name is not None:
        matches = [s["address"] for s in symbols if s["name"] == entry_name]
        if not matches:
            sys.exit(f"error: --entry symbol {entry_name!r} not found")
        entry_point = matches[0]

    return {
        "version": "mafgen-converted",
        "imageSize": max_end + 1,
        "entryPoint": entry_point,
        "sections": sections,
        "symbols": symbols,
        "modules": [{"name": "mafgen", "filename": None}],
        "relocations": [],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path, help="MAFGEN csects-*.json file")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="output sym.json path (default: <stem>.sym.json)")
    ap.add_argument("--entry", default=None, metavar="NAME",
                    help="symbol name to use as the entry point")
    args = ap.parse_args(argv)

    with open(args.input) as f:
        csects = json.load(f)
    if not isinstance(csects, dict):
        sys.exit("error: input is not a MAFGEN csects map (expected a JSON object)")

    data = convert(csects, args.entry)

    out = args.output
    if out is None:
        # csects-SSW.json -> SSW.sym.json ; foo.json -> foo.sym.json
        stem = re.sub(r"^csects-", "", args.input.stem)
        out = args.input.with_name(f"{stem}.sym.json")

    with open(out, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Wrote {out}: {len(data['sections'])} sections, "
          f"{len(data['symbols'])} symbols"
          + (f", entry={args.entry}@0x{data['entryPoint']:X}"
             if data["entryPoint"] is not None else ", no entry point"))


if __name__ == "__main__":
    main()
