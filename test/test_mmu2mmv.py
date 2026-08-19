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

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
