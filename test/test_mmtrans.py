#!/usr/bin/env python3
"""test_mmtrans -- the MMU transaction-table encoding.

Checked here: that the purge table and its message table, derived from tape
geometry alone, reproduce what the build assembled halfword for halfword.

Run:  python3 test/test_mmtrans.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tools import mmtrans as M           # noqa: E402

passed = failed = 0


def check(label, got, want):
    global passed, failed
    if got == want:
        passed += 1
    else:
        failed += 1
        print(f"FAIL  {label}: got {got!r}, want {want!r}")


def main() -> int:
    # ---- field packing, against the halfwords the member documents -------
    # FMADEU11's DCP transaction, from the MMLDRTBL DC cards.
    check("header 17 blocks", M.encodeHeader(0x2407, 17)[1], 0x0080)
    check("header no protect", M.encodeHeader(0x2407, 17)[2], M.NO_PROTECT)
    check("entry 17 blk x 512", M.encodeEntry(17, 512, 0xA000)[0], 0x87FC)
    check("entry 8 blk, 76 hw", M.encodeEntry(8, 76, 0xD000)[0], 0x392C)
    check("entry 1 blk, 256 hw, msb 1",
          M.encodeEntry(1, 256, 0x0000, addrMsb=1)[0], 0x03FD)

    # ---- tape address <-> ALLOC card ------------------------------------
    check("mm16 44007 -> card", M.fmtCard(0x2407), "44007")
    check("mm16 44308 -> card", M.fmtCard(0x2468), "44308")
    check("mm16 46000 -> card", M.fmtCard(0x2600), "46000")

    # ---- geometry: the sweep covers the whole tape exactly once ---------
    check("purge regions", M.REGIONS, 64)
    check("purge covers the tape",
          M.REGIONS * M.BLOCKS_PER_REGION, 8 * 8 * 8 * 32)

    # ---- decode/encode round trip ---------------------------------------
    w = M.purgeTable()
    t = M.decode("MMPURTBL", M.PURGE, 0, w)
    check("purge decodes its own count", t.count, M.PURGE_SLOTS)
    check("purge decodes 75 transactions", len(t.transactions), M.PURGE_SLOTS)
    live = [x for x in t.transactions if x.mm != M.SPARE]
    check("purge live regions", len(live), 64)
    check("purge first region", live[0].mm, 0x0000)
    check("purge last region", live[-1].mm, 0x3F00)
    check("purge region size", live[0].blocks, 256)

    msg = M.purgeMessages()
    check("purge message slots", len(msg), M.PURGE_SLOTS)
    check("purge live messages", msg.count(M.PURGE_LIVE_MSG), 64)
    check("purge spare messages", msg.count(M.PURGE_SPARE_MSG), 11)

    # ---- the three DEU loads, from the cards ----------------------------
    #
    # The sizes are the budget the DEU's two programs were written to.
    # ext/sim's meds/deuSASTP.coffee carries the self test's.
    con80 = ROOT / "code" / "OI340700" / "CON80"
    if con80.exists():
        hw = M.systemHwds(con80)
        # FMADEU1n is the DEU control program, FMADEU2n the stand-alone self
        # test, DMACDFTn the critical formats.
        check("DEU control program halfwords", hw["FMADEU11"], 0x2200)
        check("DEU self test halfwords", hw["FMADEU21"], 0x0e4c)
        check("critical format halfwords", hw["DMACDFT1"], 0x0e4c)
        # The self test and a critical format are the same load into the
        # same buffer: the (CRITICAL) FORMAT BUFFER, 3656 halfwords at 256,
        # plus the FORMAT CHECKSUM at 3912 -- 3657 from 0x0100 to 0x0f48 --
        # rounded up to a doubleword.
        check("self test and critical format are the same load size",
              hw["FMADEU21"], hw["DMACDFT1"])
        check("and that size is the format buffer, doubleword rounded",
              -(-3657 // 4) * 4, hw["FMADEU21"])
        # Every copy of each is identical in length, which is what makes the
        # transaction table's choice of copy immaterial.
        for stem, n in (("FMADEU1", 3), ("FMADEU2", 2), ("DMACDFT", 3)):
            sizes = {hw[f"{stem}{i}"] for i in range(1, n + 1)}
            check(f"{stem} copies are one length", len(sizes), 1)

    # ---- against the build, when there is one ---------------------------
    mmu = ROOT / "build" / "mmu"
    if (mmu / "PHASE10" / "PHASE10.sym.json").exists():
        built = {x.name: x for x in M.loadTables(mmu)}
        check("MMPURTBL reproduced",
              built["MMPURTBL"].words[:len(w)], w)
        check("MMUPURMT reproduced",
              built["MMUPURMT"].words[:len(msg)], msg)
        # Every tape address in every load table lands in a real allocation.
        allocs = M.allocations(ROOT / "code" / "OI340700" / "CON80")
        hwds = M.systemHwds(ROOT / "code" / "OI340700" / "CON80")
        span = sorted(allocs, key=lambda a: a.addr)
        unowned = []
        for tb in built.values():
            if tb.kind != M.TRANSACTION:
                continue
            for tr in tb.transactions:
                if not any(a.addr <= tr.mm < a.addr + a.blocks for a in span):
                    unowned.append((tb.name, tr.mm))
        check("every load transaction lands in an allocation", unowned, [])

        # The DEU table is one halfword away from its own copy-1 cards, and
        # that halfword is the DCP address -- see the handoff.
        dest = [x.entries[0].gpc for x in built["FMADEU11"].transactions]
        gen, _ = M.deuTable(allocs, hwds, dest, copy=1)
        got = built["FMADEU11"].words[:len(gen)]
        bad = [i for i, (g, x) in enumerate(zip(got, gen)) if g != x]
        check("DEU table differs from copy 1 in exactly one halfword",
              bad, [1])
        check("and it is the DCP tape address", (got[1], gen[1]),
              (0x2468, 0x2407))
    else:
        print("note: build/mmu/PHASE10 absent -- built-image checks skipped")

    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
