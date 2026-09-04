#!/usr/bin/env python3
"""asmimage -- assemble a standalone AP-101 program and locate it at a fixed
address, as a JSON image the simulator embeds.

asm101 assembles the source and lnk101 places the single csect at --origin,
resolving every relocatable field for that address.  The result is the
located halfwords, the symbol table, and the source it came from:

  {"name": ..., "origin": <halfword address>, "length": <halfwords>,
   "image": ["F403", ...], "symbols": {"IPLMSC": 262080, ...},
   "source": ..., "sourceSha256": ..., "toolchain": ...}

The image carries absolute addresses and runs only at --origin.

Usage:
  asmimage ext/sim/gpc/asm/fakeipl.asm --origin 0x3FF80 \
           --out ext/sim/gpc/gen/fakeipl.json \
           --listing ext/sim/gpc/gen/fakeipl.lst
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import asm101
from lnk101.repro import version_string

SRC_ROOT = Path(asm101.__file__).resolve().parents[1]


def run(module: str, args: list[str]) -> None:
    """Run one of the tools as a process: asm101 holds its literal pools
    in module state, so an interpreter assembles once."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SRC_ROOT)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    r = subprocess.run([sys.executable, "-m", module, *args], env=env,
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout)
        sys.stderr.write(r.stderr)
        raise SystemExit(f"asmimage: {module} failed ({r.returncode})")


def build(source: Path, origin: int, listing: Path | None = None,
          name: str | None = None) -> dict:
    """Assemble and locate `source` at halfword address `origin`."""
    source = Path(source).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        obj = tmp / (source.stem + ".obj")
        lst = tmp / (source.stem + ".lst")
        # asm101's .asmg.json sidecar beside the object is where lnk101
        # picks up the local symbols.
        run("asm101", [str(source), "-o", str(obj), "-l", str(lst)])
        if listing is not None:
            Path(listing).write_text(lst.read_text())

        fcm = tmp / "image.fcm"
        sym = tmp / "image.sym.json"
        run("lnk101", [str(obj), "-o", str(fcm), "-Ttext", hex(origin * 2),
                       "--json-symbols", str(sym), "--compact",
                       "--no-default-libs", "--quiet"])
        image = fcm.read_bytes()
        symbols = json.loads(sym.read_text())

    halfwords = [f"{int.from_bytes(image[i:i + 2], 'big'):04X}"
                 for i in range(0, len(image), 2)]
    sections = symbols.get("sections", [])
    return {
        "name": name or (sections[0]["name"] if sections else source.stem.upper()),
        "origin": origin,
        "length": len(halfwords),
        "image": halfwords,
        "symbols": {s["name"]: s["address"] for s in symbols.get("symbols", [])},
        "source": source.name,
        "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "toolchain": version_string(),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="asmimage",
        description="Assemble an AP-101 program and locate it at a fixed "
                    "address, as a JSON image")
    ap.add_argument("source", type=Path, help="assembler source file")
    ap.add_argument("--origin", required=True,
                    help="halfword address to locate the program at")
    ap.add_argument("--out", type=Path, required=True,
                    help="JSON image to write")
    ap.add_argument("--listing", type=Path,
                    help="also write the assembly listing here")
    ap.add_argument("--name", help="image name (default: the csect's)")
    args = ap.parse_args(argv)

    image = build(args.source, int(str(args.origin), 0), args.listing,
                  args.name)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(image, indent=2) + "\n")
    print(f"asmimage: {args.source} -> {args.out}: {image['length']} halfwords "
          f"at 0x{image['origin']:05X}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
