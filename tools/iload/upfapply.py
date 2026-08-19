#!/usr/bin/env python3
"""upfapply -- apply a Universal Patch Format card deck to a composed .fcm
image (the I-load application step of reconfiguration).

Patches are applied by PHASE / LOAD BLOCK / OFFSET
    load block = TEXT extent in the phase .lib,
    offset = halfword offset within the extent.

The deck's AP101 ADDR is cross-checked against the resolved address; a
mismatch means the deck was cut against a different relink of the phase
.lib, whose extent numbering it no longer matches, so nothing is
applied and the tool exits non-zero.  Regenerate the deck
(tools/iload/upfgen.py or upf/genupf.py) and rerun.

A deck patches one phase's tape load block.  Where a later-loaded phase
overlays that block, memory holds the overlaying phase's text and never
the patch, so the deck's content is written only where its own
phase still owns the halfword.  Ownership is mmu2fcm's `ownerPhaseRunsHW`
(compose's per-halfword load order) in the image's sym.json; the two coarser
gates below cover halfwords that map does not reach.

Usage:
  python3 tools/iload/upfapply.py --upf <deck.upf> --mmu build/mmu \
      --fcm-dir build/mmu/G16 --name G16 --phases 10,2,13,3,4 \
      --out build/mmu/G16_ILOAD
"""
import argparse
import bisect
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


def ownerFn(symPath: Path):
    """hw -> the phase whose text the composed image holds there (0 for
    background fill, -1 for a compose-time pseudo phase), or None when the
    image's sym.json carries no ownership map."""
    runs = json.loads(symPath.read_text()).get("ownerPhaseRunsHW")
    if not runs:
        return None
    starts = [r[0] for r in runs]
    values = [r[1] for r in runs]

    def owner(hw: int) -> int:
        return values[bisect.bisect_right(starts, hw) - 1]
    return owner


def lbSlotFn(symPath: Path):
    """hw -> the MMUSYS-assigned phase whose tape load block closes there,
    or None when the image's sym.json carries no slot map.

    The gate for the two halfwords the other two cannot see: a block's
    trailing checksum slot is written from its phase's tape record although
    no csect covers it and no .lib extent supplies it, so neither
    `ownerPhaseRunsHW` nor `coversFn` names a phase there.  Only phases
    MMUSYS assigns to the configuration are listed -- a block closed by an
    IPL-set phase keeps the IPL record's value."""
    rows = json.loads(symPath.read_text()).get("lbSlotPhases")
    if not rows:
        return None
    table = {a: p for a, p in rows}
    return table.get


def coversFn(mmu: Path, phases):
    """hw -> the set of phases whose csect map covers it.

    A coarser gate than `ownerPhaseRunsHW`, which is built from the phases'
    `.lib` extents: a csect the linker resolves through another module (the
    `module` field ending in '>') contributes no extent, so the owner map
    never names the covering phase even though that phase's tape record does
    write the halfword.  Coverage comes from the csect map instead, and the
    last phase in load order that covers a halfword owns it.
    """
    spans = []          # (start, end, phase)
    for ph in phases:
        p = mmu / f"PHASE{ph:02d}" / f"PHASE{ph:02d}.sym.json"
        if not p.exists():
            continue
        for s in json.loads(p.read_text()).get("sections", []):
            if s.get("size"):
                spans.append((s["address"], s["address"] + s["size"], ph))
    spans.sort()
    starts = [s[0] for s in spans]

    def covers(hw: int) -> set:
        # spans are short and few; scan back from the first start > hw
        out = set()
        i = bisect.bisect_right(starts, hw)
        for a, b, ph in spans[max(0, i - 64):i]:
            if a <= hw < b:
                out.add(ph)
        return out
    return covers


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

    order = {int(p): i for i, p in enumerate(a.phases.split(","))}
    phases = set(order)
    image = bytearray((a.fcm_dir / f"{a.name}.fcm").read_bytes())
    owner = ownerFn(a.fcm_dir / f"{a.name}.sym.json")
    if owner is None:
        print(f"WARNING {a.fcm_dir / f'{a.name}.sym.json'} carries no "
              f"ownerPhaseRunsHW -- applying without the overlay gate "
              f"(recompose with a current mmu2fcm)", file=sys.stderr)
    covers = coversFn(a.mmu, sorted(order, key=order.get))
    lbSlot = lbSlotFn(a.fcm_dir / f"{a.name}.sym.json")

    extCache = {}
    def extent(phase, lb):
        if phase not in extCache:
            extCache[phase] = LibModule.read(
                a.mmu / f"PHASE{phase:02d}.lib").extents
        return extCache[phase][lb - 1]

    stats = defaultdict(int)
    runs = []           # (hw, words) resolved and address-verified
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
        runs.append((phase, hw, words))

    if mismatches:
        print(f"STALE DECK {a.upf}: {len(mismatches)} of "
              f"{len(runs)} runs resolve PHASE/LB/OFFSET to an address "
              f"other than their AP101 ADDR -- the deck's load-block "
              f"geometry predates the current phase .lib relink.  "
              f"Nothing applied.  Regenerate the deck "
              f"(tools/iload/upfgen.py or upf/genupf.py) and rerun.",
              file=sys.stderr)
        for phase, lb, off, hw, addr in mismatches[:8]:
            print(f"  phase {phase} lb {lb} offset {off}: "
                  f"resolves {hw:05X} vs deck AP101 ADDR {addr:05X}",
                  file=sys.stderr)
        raise SystemExit(2)

    for phase, hw, words in runs:
        applied = 0
        for i, w in enumerate(words):
            if owner is not None:
                o = owner(hw + i)
                # 0 = background/staging fill, which the deck's own block
                # still delivers; a later-loaded phase (or a compose-time
                # pseudo phase, always last) owns the memory instead, so its
                # text stands there and not the patch
                if o and order.get(o, len(order)) > order[phase]:
                    stats["overlaidHw"] += 1
                    continue
            # csect-coverage gate (see coversFn): a phase whose csect map
            # covers the halfword writes it from tape even with no extent of
            # its own, and a later such phase overwrites this deck's phase
            if covers is not None:
                later = [p for p in covers(hw + i)
                         if order.get(p, -1) > order[phase]]
                if later:
                    stats["coveredHw"] += 1
                    continue
            # load-block slot gate (see lbSlotFn): a later phase's block
            # closes here, so memory holds its checksum
            if lbSlot is not None:
                p = lbSlot(hw + i)
                if p is not None and order.get(p, -1) > order[phase]:
                    stats["slotHw"] += 1
                    continue
            image[(hw + i) * 2] = w >> 8
            image[(hw + i) * 2 + 1] = w & 0xFF
            applied += 1
        stats["appliedRuns"] += bool(applied)
        stats["appliedHw"] += applied

    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / f"{a.name}.fcm").write_bytes(image)
    symSrc = a.fcm_dir / f"{a.name}.sym.json"
    symDst = a.out / f"{a.name}.sym.json"
    if not (symDst.exists() and symSrc.samefile(symDst)):
        shutil.copy(symSrc, symDst)      # chained decks: --fcm-dir == --out
    print(f"applied {stats['appliedRuns']} runs / {stats['appliedHw']} hw; "
          f"skipped (other phases) {stats['skippedRuns']} runs / "
          f"{stats['skippedHw']} hw; "
          f"not in memory (later phase overlays the block) "
          f"{stats['overlaidHw']} hw; "
          f"later phase's block checksum {stats['slotHw']} hw")
    print(f"wrote {a.out / f'{a.name}.fcm'}")


if __name__ == "__main__":
    main()
