#!/usr/bin/env python3
"""test_mmustamp -- the Mass-Memory-Build tables written into the load modules.

Checked here: who owns a table, that the sidecar keeps the linkedit
content, and that restore inverts a stamp.

Run:  python3 test/test_mmustamp.py
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ap101Utils import mmbstamp as MB           # noqa: E402
from ap101Utils.libModule import Extent, LibModule   # noqa: E402

passed = failed = 0


def check(label, got, want):
    global passed, failed
    if got == want:
        passed += 1
    else:
        failed += 1
        print(f"FAIL  {label}: got {got!r}, want {want!r}")


def ok(label, cond):
    check(label, bool(cond), True)


GPT_AT, CPHA_AT, G3_AT = 0x1000, 0x2000, 0x3000
BASE, SPAN = 0x0800, 0x3000        # halfwords: one extent over all three


def tables(seed=0):
    """Generated values, distinguishable per table and per run."""
    from ap101Utils.g3dat import G3DAT_SIZE
    return MB.PhaseTables(
        gpt=[(seed + i) & 0xFFFF or 1 for i in range(MB.GPT_SIZE)],
        cpha=[(seed + 0x100 + i) & 0xFFFF or 1 for i in range(MB.CPHA_SIZE)],
        g3dat=[(seed + 0x200 + i) & 0xFFFF or 1 for i in range(G3DAT_SIZE)])


def hwAt(lib, address, count):
    x = MB._extent_for(lib, address, count)
    off = 2 * (address - x.address // 2)
    return [int.from_bytes(x.data[off + 2 * i:off + 2 * i + 2], "big")
            for i in range(count)]


def phaseTree(root: Path, carriers=(2,), declarers=(2, 3, 12)):
    """A tree of phase modules: every `declarers` manifest names the three
    csects, only `carriers` modules have text over them."""
    from ap101Utils.g3dat import G3DAT_CSECT, G3DAT_SIZE
    secs = [{"name": MB.GPT_CSECT, "address": GPT_AT, "size": MB.GPT_SIZE},
            {"name": MB.CPHA_CSECT, "address": CPHA_AT, "size": MB.CPHA_SIZE},
            {"name": G3DAT_CSECT, "address": G3_AT, "size": G3DAT_SIZE}]
    for n in sorted(set(declarers) | set(carriers)):
        lib = LibModule(name=f"PHASE{n:02d}")
        if n in carriers:
            # Non-zero fill, so "the stamp wrote something" is not the same
            # as "the module was zeros all along".
            lib.extents = [Extent(address=2 * BASE,
                                  data=bytes([0xA5, 0x5A]) * SPAN)]
        else:
            lib.extents = [Extent(address=2 * (BASE + SPAN + 0x10),
                                  data=b"\x00\x01" * 8)]
        lib.write(root / f"PHASE{n:02d}.lib")
        d = root / f"PHASE{n:02d}"
        d.mkdir(exist_ok=True)
        (d / f"PHASE{n:02d}.sym.json").write_text(
            json.dumps({"sections": secs if n in declarers or n in carriers
                        else []}))


def main() -> int:
    from ap101Utils.g3dat import G3DAT_CSECT, G3DAT_SIZE

    # -- the two stampers agree on what there is to stamp -------------------
    t = tables()
    check("three tables when G3DAT generated",
          [n for n, _, _ in MB.table_csects(t)],
          [MB.GPT_CSECT, MB.CPHA_CSECT, G3DAT_CSECT])
    check("two when it was not",
          [n for n, _, _ in MB.table_csects(
              MB.PhaseTables(gpt=t.gpt, cpha=t.cpha))],
          [MB.GPT_CSECT, MB.CPHA_CSECT])

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        phaseTree(root)
        pristine = LibModule.read(root / "PHASE02.lib").extents[0].data

        # -- ownership: declared by three phases, carried by one ------------
        st = MB.stamp_libs(root, t)
        check("one stamp per table", len(st), 3)
        check("all of them in the module that carries them",
              sorted({s.phase for s in st}), [2])
        check("and named in table order", [s.csect for s in st],
              [MB.GPT_CSECT, MB.CPHA_CSECT, G3DAT_CSECT])

        lib = LibModule.read(root / "PHASE02.lib")
        check("the phase table landed", hwAt(lib, GPT_AT, MB.GPT_SIZE), t.gpt)
        check("the alternate-area table landed",
              hwAt(lib, CPHA_AT, MB.CPHA_SIZE), t.cpha)
        check("the archive table landed", hwAt(lib, G3_AT, G3DAT_SIZE),
              t.g3dat)
        ok("a phase that only declares them is untouched",
           LibModule.read(root / "PHASE03.lib").extents[0].data
           == LibModule.read(root / "PHASE12.lib").extents[0].data)
        ok("no sidecar for a phase that carries nothing",
           not MB.sidecar_path(root / "PHASE03.lib").exists())

        # -- the sidecar holds the LINKEDIT content -------------------------
        side = MB.sidecar_path(root / "PHASE02.lib")
        ok("the carrier gets a sidecar", side.exists())
        rec = json.loads(side.read_text())
        check("recording every table", len(rec["tables"]), 3)
        check("with what the linkedit left, not what we wrote",
              rec["tables"][0]["before"], [0xA55A] * MB.GPT_SIZE)

        # -- re-stamping is idempotent and does not eat the baseline --------
        t2 = tables(seed=0x40)
        MB.stamp_libs(root, t2)
        check("a second stamp overwrites the values",
              hwAt(LibModule.read(root / "PHASE02.lib"), GPT_AT, MB.GPT_SIZE),
              t2.gpt)
        check("but the sidecar still holds the linkedit content",
              json.loads(side.read_text())["tables"][0]["before"],
              [0xA55A] * MB.GPT_SIZE)

        # -- restore is a true inverse --------------------------------------
        back = MB.restore_libs(root)
        check("restore reports every table", len(back), 3)
        check("the module is byte-identical to the linkedit output",
              LibModule.read(root / "PHASE02.lib").extents[0].data, pristine)
        ok("and the sidecar is gone", not side.exists())
        check("restoring an unstamped tree does nothing",
              MB.restore_libs(root), [])

        # -- --report changes nothing ---------------------------------------
        MB.stamp_libs(root, t, dry_run=True)
        check("a dry run leaves the module alone",
              LibModule.read(root / "PHASE02.lib").extents[0].data, pristine)
        ok("and writes no sidecar", not side.exists())

    # -- an owner has to be exactly one module ------------------------------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        phaseTree(root, carriers=(2, 4))
        try:
            MB.stamp_libs(root, tables())
            failed_ok = False
        except MB.StampError as e:
            failed_ok = "carried by phases 2, 4" in str(e)
        ok("two modules carrying one table is an error", failed_ok)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        phaseTree(root, carriers=(), declarers=(3,))
        try:
            MB.stamp_libs(root, tables())
            failed_ok = False
        except MB.StampError as e:
            failed_ok = "carries" in str(e) and "nowhere to go" in str(e)
        ok("declared everywhere and carried nowhere is an error", failed_ok)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        try:
            MB.stamp_libs(root, tables())
            failed_ok = False
        except MB.StampError as e:
            failed_ok = "no PHASE*.lib" in str(e)
        ok("an empty tree is an error", failed_ok)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
