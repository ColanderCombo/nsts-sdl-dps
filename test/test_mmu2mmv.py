#!/usr/bin/env python3
"""test_mmu2mmv -- the tape volume writer, and that ext/sim reads it back.

Run:  python3 test/test_mmu2mmv.py
"""
import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tools import mmu2mmv as M           # noqa: E402

passed = failed = 0


def check(label, got, want):
    global passed, failed
    if got == want:
        passed += 1
    else:
        failed += 1
        print(f"FAIL  {label}: got {got!r}, want {want!r}")


def main() -> int:
    global passed
    # ---- geometry ----
    check("a tape holds 16384 blocks", M.BLOCKS_TOTAL, 8 * 8 * 8 * 32)
    check("which is 8.4 million halfwords",
          M.BLOCKS_TOTAL * M.HW_PER_BLOCK, 8_388_608)

    # ---- addressing ----
    # An ALLOC card writes the address as FTSBB, file first; the position
    # word a unit reports puts track first.  Both are used and they must
    # not be allowed to drift into each other.
    from ap101Utils.mmbstamp import mm16
    check("ALLOC 44000 is track 4, file 4", M.fromMm16(mm16("44000")), (4, 4, 0, 0))
    check("ALLOC 43000 is track 3, file 4", M.fromMm16(mm16("43000")), (3, 4, 0, 0))
    check("ALLOC 31728 is track 1, file 3, subfile 7, block 28",
          M.fromMm16(mm16("31728")), (1, 3, 7, 28))
    for card in ("00000", "44000", "31728", "70731"):
        t, f, s, b = M.fromMm16(mm16(card))
        check(f"{card} round trips through the card form", M.fmtCard(t, f, s, b), card)

    check("the first block is index 0", M.blockIndex(0, 0, 0, 0), 0)
    check("a subfile is 32 blocks", M.blockIndex(0, 0, 1, 0), 32)
    check("a file/track pair is 8 subfiles", M.blockIndex(1, 0, 0, 0), 256)
    check("a file is that on all 8 tracks", M.blockIndex(0, 1, 0, 0), 2048)
    # The block index is the tape address halfword.
    for card in ("00000", "44000", "43000", "31728", "70731"):
        t, f, s, b = M.fromMm16(mm16(card))
        check(f"{card} indexes the block its address names",
              M.blockIndex(t, f, s, b), mm16(card))

    # The check that settles the order: laid out this way the ground
    # build's own allocations tile the tape with no overlap at all.
    from ap101Utils.mmbstamp import _ALLOC_RE
    con80 = ROOT / "code" / "OI340700" / "CON80"
    spans = []
    for area in range(9):
        p = con80 / f"MMUDAT{area}"
        if not p.exists():
            continue
        for ln in p.read_text(errors="replace").splitlines():
            m = _ALLOC_RE.match(ln)
            if m and m.group(3):
                st = M.blockIndex(*M.fromMm16(mm16(m.group(2))))
                spans.append((st, st + int(m.group(3)), m.group(1)))
    spans.sort()
    clashes = sum(1 for i in range(1, len(spans)) if spans[i][0] < spans[i - 1][1])
    check("every ground allocation fits on the tape",
          sum(1 for s in spans if s[1] > M.BLOCKS_TOTAL), 0)
    check("...and none of them overlap", clashes, 0)
    check("...over a useful number of them", len(spans) > 150, True)
    for idx in (0, 1, 31, 32, 255, 256, 2047, 2048, 16383):
        check(f"index {idx} round trips through an address",
              M.blockIndex(*M.blockAddr(idx)), idx)

    # ---- the closing checksum, as the loader verifies it ----
    #
    # The System Software Loader sums a load block's halfwords 0..L-2 as
    # 16 bits and compares the total with halfword L-1: the slot is a
    # fullword, its first halfword is zero and contributes nothing, and
    # the sum therefore runs over everything the block carries.  Written
    # any other way the load reads correctly and is then rejected.
    from ap101Utils.concard import ConcardDeck
    from ap101Utils import mmbstamp
    from ap101Utils.mmbstamp import load_phase_source, phase_load_blocks
    deck = ConcardDeck(con80)
    src = load_phase_source(con80)
    mmuRoot = ROOT / "build" / "OI340700"
    #
    # Every built phase.  The partition the record is written from and the
    # partition the in-core phase table is stamped from are the same one,
    # and they part company only where `mmbstamp.fit_budget` has something
    # to do -- a phase with no slack against its `ALLOC BLKS`.
    area1 = mmbstamp.parse_area(con80, 1)
    seen = badsum = badfit = badpart = 0
    for phase in sorted(area1.mm_of_phase):
        if not (mmuRoot / f"PHASE{phase:02d}.lib").exists():
            continue
        if phase not in range(3, 19) and phase != 2:
            continue                        # the phase table covers 3..18
        budget = area1.blks_of_phase.get(phase)
        rec = M.phaseRecord(mmuRoot, deck, phase, src.ipl, src.mc,
                            area1.mm_of_phase[phase], budget)
        allblks, _mm, ncont, _crossed = phase_load_blocks(mmuRoot, phase, src)
        lbs = [lb for lb in allblks if not lb.reserve]
        # The record is what the phase table describes: NUM_CONT blocks,
        # counting the ones `pack_mm` steps over at a track boundary.
        if len(rec) // M.HW_PER_BLOCK != ncont:
            badpart += 1
        off = 0
        for lb in lbs:
            if lb.sot:                       # pushed to the start of a track
                while (off // M.HW_PER_BLOCK + area1.mm_of_phase[phase]) & 0xFF:
                    off += M.HW_PER_BLOCK
            blk = rec[off:off + lb.length]
            total = 0
            for i in range(lb.length - 1):
                total = (total + blk[i]) & 0xFFFF
            if total != blk[lb.length - 1] or blk[lb.length - 2] != 0:
                badsum += 1
            seen += 1
            off += lb.length
            while off % M.HW_PER_BLOCK:      # a load block starts a tape block
                off += 1
        if off != len(rec):
            badpart += 1                     # the two partitions disagree
        if len(rec) % M.HW_PER_BLOCK:
            badfit += 1
        if budget is not None and len(rec) // M.HW_PER_BLOCK > budget:
            badfit += 1
    check("every load block closes on the checksum the loader computes",
          badsum, 0)
    check("...over a useful number of them", seen > 200, True)
    check("...the record is built from the partition the phase table names",
          badpart, 0)
    check("...and holds the phase inside its allocation, whole tape blocks",
          badfit, 0)

    # ---- the volume file ----
    vol = M.Volume()
    a = (5, 2, 6, 17)
    words = [(i * 7 + 1) & 0xFFFF for i in range(M.HW_PER_BLOCK)]
    vol.write(M.blockIndex(*a), words)
    vol.write(M.blockIndex(0, 0, 0, 0), [1, 2, 3])       # short: zero filled
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.mmv"
        n = vol.save(path)
        raw = path.read_bytes()
        check("the volume file carries the magic", raw[:8], b"MMUVOL01")
        blockSize, entries, flags = struct.unpack_from(">III", raw, 8)
        check("...its block size", blockSize, 512)
        check("...one directory entry per block written", entries, 2)
        check("...and is not write protected", flags, 0)
        check("...at the size the header implies", n,
              32 + entries * 4 + entries * 512 * 2)
        # The directory is ascending, and the data follows it in the same
        # order -- which is what ext/sim/mmu/volume.coffee assumes.
        first, second = struct.unpack_from(">II", raw, 32)
        check("the directory is in tape order", first < second, True)
        check("...starting at the lowest block", first, 0)

        # ---- and ext/sim reads it back ----
        sim = ROOT / "ext" / "sim"
        if (sim / "dist" / "mmu.js").exists():
            out = subprocess.run(
                ["node", str(sim / "dist" / "mmu.js"), "dump", str(path),
                 "/".join(str(x) for x in a), "--words", "8"],
                capture_output=True, text=True, cwd=sim)
            check("the simulator reads the volume", out.returncode, 0)
            body = out.stdout
            check("...at the address it was written to",
                  body.splitlines()[0].startswith("5/2/6/17"), True)
            check("...with the halfwords that were written",
                  "0001 0008 000f 0016" in body, True)
        else:
            print("SKIP  ext/sim/dist/mmu.js not built "
                  "(cd ext/sim && npm run mmu:build)")

    # ---- LOADMOD members ----
    # A member is placed by the ALLOC of the function whose cards carry its
    # LOADMOD statement, which is in a different member from the LOADMOD
    # itself, so the join is what has to be right.
    con80 = ROOT / "code" / "OI340700" / "CON80"
    if con80.is_dir():
        from tools.mmubuild import MMUDeck, build as mmuTree
        places = mmuTree(MMUDeck(con80)).loadmod_allocations()
        cf = [p for p in places if p.member == "DEUCFLM"]
        check("the DEU critical formats are carried in three copies",
              len(cf), 3)
        check("...the first at the address GPCIPL reads",
              sorted(p.addr for p in cf), ["44408", "44416", "44424"])
        check("...eight blocks each", {p.blocks for p in cf}, {8})
        check("...with the length the SYSTEM card declares",
              {p.halfwords for p in cf}, {0xE4C})
        check("...under their own functions",
              sorted(p.label for p in cf),
              ["DMACDFT1", "DMACDFT2", "DMACDFT3"])

    # The record: image, pad to HWDS, and the closing (0000, sum16) slot
    # the reader verifies.  A bare image is re-read forever.
    with tempfile.TemporaryDirectory() as td:
        img = Path(td) / "M.bin"
        img.write_bytes(struct.pack(">3H", 0x1111, 0x2222, 0x3333))
        rec = M.loadmodRecord(img, 1, 8)
        check("a record is HWDS halfwords, then allocation fill",
              rec[:9], [0x1111, 0x2222, 0x3333, M.STAGING_FILL, M.STAGING_FILL,
                        M.STAGING_FILL, 0x0000,
                        (0x1111 + 0x2222 + 0x3333 + 3 * M.STAGING_FILL) & 0xFFFF,
                        M.STAGING_FILL])
        check("...and fills its whole allocation", len(rec), M.HW_PER_BLOCK)
        # No HWDS: the record is the image, padded even, plus the slot.
        rec = M.loadmodRecord(img, 1, None)
        check("without HWDS the slot follows the image",
              rec[3:6], [M.STAGING_FILL, 0x0000,
                         (0x1111 + 0x2222 + 0x3333 + M.STAGING_FILL) & 0xFFFF])
        # An HWDS too small for the image is not fatal -- the record grows
        # to hold it and the caller warns -- but an image too big for the
        # ALLOCATION has nowhere to go.
        check("a short HWDS still leaves room for the slot",
              len(M.loadmodRecord(img, 1, 4)), M.HW_PER_BLOCK)
        big = Path(td) / "big.bin"
        big.write_bytes(b"\x00" * (2 * M.HW_PER_BLOCK))
        try:
            M.loadmodRecord(big, 1, None)
            check("an image too big for its allocation is refused", True, False)
        except ValueError:
            passed += 1

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
