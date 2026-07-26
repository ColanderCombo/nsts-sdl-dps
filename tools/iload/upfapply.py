#!/usr/bin/env python3
"""upfapply -- apply a Universal Patch Format card deck to a composed .fcm
memory-configuration image (the I-load application step of reconfiguration).

Patches are applied by PHASE / LOAD BLOCK / OFFSET, the tape's own addressing
(load block = TEXT extent ordinal in our phase .lib, offset = halfword offset
within the extent), so application is correct even where our build layout is
not address-exact to flight.  The deck's AP101 ADDR (flight address of run
start) is cross-checked against the resolved address and mismatches are
reported -- they mark layout drift, not application errors.

Usage:
  python3 tools/iload/upfapply.py --upf <deck.upf> --mmu build/mmu \
      --fcm-dir build/mmu/G16 --name G16 --phases 10,2,13,3,4 \
      --out build/mmu/G16_ILOAD
"""
import argparse
import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from ap101Utils.libModule import LibModule  # noqa: E402


def parseDeck(path: Path):
    """Yield (phase, lb, offset, nwords, ap101Addr, sp, [words])."""
    phase = lb = None
    pending = None      # (offset, nwords, addr, sp, words) awaiting E cards
    for ln in path.read_text().splitlines():
        ctype = ln[70:71]
        if ctype == "C":
            m = re.search(r"PHASE,LB\s+(\d+),(\d+)", ln)
            phase, lb = int(m.group(1)), int(m.group(2))
        elif ctype == "D":
            if pending:
                yield pending
            m = re.match(r"OFFSET\s+(\d+)\s+NUMBER\s+(\d+)\s+"
                         r"AP101 ADDR\s+([0-9A-F]+)\s+SP\s+(\d)", ln)
            pending = (phase, lb, int(m.group(1)), int(m.group(2)),
                       int(m.group(3), 16), int(m.group(4)), [])
        elif ctype == "E":
            pending[6].extend(int(w, 16) for w in ln[11:64].split())
    if pending:
        yield pending


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upf", type=Path, required=True)
    ap.add_argument("--mmu", type=Path, required=True)
    ap.add_argument("--fcm-dir", type=Path, required=True,
                    help="dir holding <name>.fcm + <name>.sym.json")
    ap.add_argument("--name", required=True,
                    help="image basename inside --fcm-dir (e.g. G16)")
    ap.add_argument("--phases", required=True,
                    help="comma list of phases composing this image; "
                         "patches for other phases are skipped")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    phases = {int(p) for p in a.phases.split(",")}
    image = bytearray((a.fcm_dir / f"{a.name}.fcm").read_bytes())

    extCache = {}
    def extent(phase, lb):
        if phase not in extCache:
            extCache[phase] = LibModule.read(
                a.mmu / f"PHASE{phase:02d}.lib").extents
        return extCache[phase][lb - 1]

    stats = defaultdict(int)
    mismatches = []
    for phase, lb, offset, nwords, addr, sp, words in parseDeck(a.upf):
        if len(words) != nwords:
            raise SystemExit(f"deck error: run at phase {phase} lb {lb} "
                             f"offset {offset}: {len(words)} != {nwords}")
        if phase not in phases:
            stats["skippedRuns"] += 1
            stats["skippedHw"] += nwords
            continue
        ext = extent(phase, lb)
        hw = ext.address // 2 + offset
        if hw != addr:
            mismatches.append((phase, lb, offset, hw, addr))
        for i, w in enumerate(words):
            image[(hw + i) * 2] = w >> 8
            image[(hw + i) * 2 + 1] = w & 0xFF
        stats["appliedRuns"] += 1
        stats["appliedHw"] += nwords

    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / f"{a.name}.fcm").write_bytes(image)
    shutil.copy(a.fcm_dir / f"{a.name}.sym.json", a.out / f"{a.name}.sym.json")
    print(f"applied {stats['appliedRuns']} runs / {stats['appliedHw']} hw; "
          f"skipped (other phases) {stats['skippedRuns']} runs / "
          f"{stats['skippedHw']} hw")
    if mismatches:
        print(f"NOTE: {len(mismatches)} runs where our address != flight "
              f"AP101 ADDR (layout drift; applied at our address):")
        for phase, lb, off, hw, addr in mismatches[:8]:
            print(f"  phase {phase} lb {lb} offset {off}: "
                  f"ours {hw:05X} vs flight {addr:05X}")
    print(f"wrote {a.out / f'{a.name}.fcm'}")


if __name__ == "__main__":
    main()
