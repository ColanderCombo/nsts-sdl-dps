#!/usr/bin/env python3
"""cksstamp -- re-stamp the load-block checksums after the patch chain.

A tape load block's trailing 2-hw slot is (0000, sum16 of the block), summed
over the block as written to tape, i.e. with the UPF decks and the I-load
cards already merged in.  `mmu2fcm --stamp-checksums` runs at compose time,
before those merges, so every block a patch lands in carries a load-module sum
where a tape sum belongs.  This tool is the last step of the chain and
re-stamps exactly those blocks.

Rules:

  * the sum is over the phase's own .lib TEXT with the C6C6 staging fill in
    every gap, not over the composed image: a later phase may overlay parts of
    an earlier phase's blocks, but the tape record still carries the earlier
    phase's own text.
  * a patch delta counts for a block only where the patched halfword is in
    that phase's text and the deck was cut for that phase, which it names on
    its C card (`PHASE,LB nnn,nnn`).  An overlaying phase's block therefore
    ignores a deck written against the phase underneath it.  The delta below
    is image-minus-base, so a halfword a deck had written into a later phase's
    overlay would contribute the difference between two phases' records, which
    belongs to neither block's sum; upfapply's overlay gate keeps that from
    arising by applying a deck only where its own phase still owns the
    composed halfword.
  * a block's own trailing slot is never content.
  * the slot is written only where the composed image still shows it.  The
    tape record carries the checksum, but memory at that halfword holds
    whatever the last phase to load wrote there, so stamping an overlaid slot
    would put a tape artefact into another phase's data.  The (0000, sum)
    guard below cannot catch that on its own -- an overlaid all-zero array is
    indistinguishable from an unstamped slot.
  * a slot some later step wrote itself is left alone: the IPL_CHECKSUM_SLOTS
    deck supplies 10 slots verbatim and those are authoritative.  The guard is
    that the slot must still hold exactly what mmu2fcm computed for it --
    (0000, load-module sum).

Usage (after upfapply and iloadcards, before rendering the listing):
  python3 tools/iload/cksstamp.py --mmu build/mmu --con80 code/OI340700/CON80 \\
      --name SSW --phases 10,2,13,3 --base build/mmu/SSW \\
      --fcm-dir build/mmu/SSW_OI3407 --out build/mmu/SSW_OI3407 \\
      --upf <deck.upf> ...
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from ap101Utils import mmbstamp                # noqa: E402
from ap101Utils.libModule import LibModule     # noqa: E402

# UPF C card names the phase the deck patches; D cards give the run.
_C = re.compile(r"PHASE,LB\s+(\d+),(\d+)")
_D = re.compile(r"OFFSET\s+(\d+)\s+NUMBER\s+(\d+)\s+AP101 ADDR\s+([0-9A-F]+)")


def deckPhases(paths) -> dict:
    """hw address -> (phase its UPF deck declares it against, deck word).

    The word matters, not just the address.  A deck run covers a whole span
    but re-supplies most of it unchanged, and upfapply gates a deck: where a
    later phase covers the halfword the deck's word is never written at all.
    An address inside a deck's range therefore does not make that deck the
    writer -- an I-load card, which is name-based and carries no phase of its
    own, can be the one that wrote it.  The caller honours the attribution
    only where the deck's own word is the one the image holds.
    """
    out: dict[int, tuple[int, int | None]] = {}
    for p in paths:
        phase = None
        pending: list | None = None       # [addr, nwords, [words]]

        def flush():
            if pending is None:
                return
            addr, n, words = pending
            for i in range(n):
                out[addr + i] = (phase, words[i] if i < len(words) else None)

        for ln in Path(p).read_text().splitlines():
            ctype = ln[70:71]
            if ctype == "C":
                m = _C.search(ln)
                if m:
                    flush()
                    pending = None
                    phase = int(m.group(1))
            elif ctype == "D":
                m = _D.match(ln)
                if m and phase is not None:
                    flush()
                    pending = [int(m.group(3), 16), int(m.group(2)), []]
            elif ctype == "E" and pending is not None:
                pending[2].extend(int(w, 16) for w in ln[11:64].split())
        flush()
    return out


def phaseText(mmu: Path, n: int) -> dict:
    """PHASEnn's staged halfwords: hw address -> value.

    mmbstamp.tape_text is the single definition, and it drops the
    auto-generated @-stacks: they are allocation with no tape text, so the MMB
    staging fill covers them and this sum must too.  mmu2fcm
    --stamp-checksums applies the same rule; disagreeing here would make the
    guard below reject every block that holds a stack."""
    return mmbstamp.tape_text(mmu, n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mmu", type=Path, required=True)
    ap.add_argument("--con80", type=Path, required=True)
    ap.add_argument("--name", required=True, help="image basename, e.g. SSW")
    ap.add_argument("--phases", required=True,
                    help="comma list of phases composing this image")
    ap.add_argument("--base", type=Path, required=True,
                    help="dir holding the UNPATCHED compose of the same "
                         "config -- the delta baseline")
    ap.add_argument("--fcm-dir", type=Path, required=True,
                    help="dir holding the patched <name>.fcm")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--upf", type=Path, action="append", default=[],
                    help="every UPF deck the chain applied (repeatable); "
                         "their C cards give the per-phase attribution")
    a = ap.parse_args()

    src = mmbstamp.load_phase_source(a.con80)
    phases = [int(p) for p in a.phases.split(",")]
    upfPhase = deckPhases(a.upf)

    image = bytearray((a.fcm_dir / f"{a.name}.fcm").read_bytes())
    base = (a.base / f"{a.name}.fcm").read_bytes()
    nhw = len(image) // 2

    def get(buf, h):
        return (buf[2 * h] << 8) | buf[2 * h + 1]

    blocks, text = [], {}
    for n in phases:
        try:
            lbs = mmbstamp.phase_load_blocks(a.mmu, n, src)[0]
        except mmbstamp.StampError:
            continue                  # unassigned phase: no tape records
        text[n] = phaseText(a.mmu, n)
        blocks += [(n, lb.start, lb.length) for lb in lbs]
    slots = {s + ln - 2 for _, s, ln in blocks}
    slots |= {s + ln - 1 for _, s, ln in blocks}
    # phases compose in --phases order, so a later one in the list overlays
    # an earlier one wherever it has text of its own
    rank = {n: i for i, n in enumerate(phases)}

    def overlaid(n, slot):
        """The later-loaded phase whose own text covers either halfword of
        this block's slot pair, or None.  Memory shows that phase's data
        there, not this block's checksum."""
        for p, t in text.items():
            if rank.get(p, -1) <= rank.get(n, -1):
                continue
            if slot in t or slot + 1 in t:
                return p
        return None

    rewrote = skipped = overlaidN = 0
    for n, start, length in blocks:
        slot = start + length - 2
        if slot + 2 > nhw:
            continue
        t = text[n]
        orig = 0
        for h in range(start, slot):
            orig += t.get(h, mmbstamp.FILL)
        orig &= 0xFFFF
        if get(image, slot) != 0 or get(image, slot + 1) != orig:
            skipped += 1          # unstamped, or a later step owns the slot
            continue
        later = overlaid(n, slot)
        if later is not None:
            overlaidN += 1
            print(f"  PHASE{n:02d} block {start:#07x}+{length} slot "
                  f"{slot:#07x}: NOT stamped -- PHASE{later:02d} overlays it")
            continue
        delta = 0
        for h in range(start, slot):
            if h in slots or h not in t:
                continue
            dp, dw = upfPhase.get(h, (n, None))
            if dp != n and dw is not None and dw == get(image, h):
                continue          # the deck patched another phase's record
            delta += get(image, h) - get(base, h)
        if not delta:
            continue
        new = (orig + delta) & 0xFFFF
        image[2 * slot + 2:2 * slot + 4] = new.to_bytes(2, "big")
        rewrote += 1
        print(f"  PHASE{n:02d} block {start:#07x}+{length} slot {slot:#07x}: "
              f"{orig:04X} -> {new:04X}")

    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / f"{a.name}.fcm").write_bytes(image)
    print(f"re-stamped {rewrote} load-block checksums over the patch chain "
          f"({skipped} slots left to their own writer, "
          f"{overlaidN} overlaid by a later phase)")
    print(f"wrote {a.out / f'{a.name}.fcm'}")


if __name__ == "__main__":
    main()

