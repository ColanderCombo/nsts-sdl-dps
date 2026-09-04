#!/usr/bin/env python3
"""mmu2mmv -- write the built phases onto a mass memory tape volume (.mmv).

An ALLOC card gives a member's tape address as five decimal digits FTSBB
-- file, track, subfile, and a two-digit block -- and the number of
512-halfword blocks it may occupy; a PHASE card says which phase that
member is.  This joins the two, finds each phase's linked load module
under the con80build output, and lays its tape record into its allocation.

A phase's tape record is its load blocks back to back, each running from
its first halfword to a closing 2-halfword checksum slot.  The Mass Memory
Build stages a block from an INIT=C6C6 buffer, so every halfword the phase
does not supply is C6C6, and the slot carries (0000, sum16 of the block
before it).

`--loadmod NAME=FILE` supplies a LOADMOD member's image as raw big-endian
halfwords, written into every allocation naming it.  The IPL bootstrap
copy is written from PHASE01; see `bootRecord`.

Not generated: the mass memory directory, the transaction and message
tables.  `--report` lists every allocation and says which were filled.

Usage:
  mmu2mmv --con80 code/OI340700/CON80 --mmu build/mmu --out tape.mmv
  mmu2mmv --loadmod DEUCFLM=build/OI340700/DEUCFLM.bin --out tape.mmv
  mmu2mmv --report
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import struct
import sys
from pathlib import Path

from ap101Utils import mmbstamp
from ap101Utils.concard import ConcardDeck
from ap101Utils.libModule import LibModule

log = logging.getLogger("mmu2mmv")

# Tape geometry.  8 tracks x 8 files x 8 subfiles x 32 blocks of 512
# halfwords is 8,388,608 halfwords, the unit's documented capacity.
HW_PER_BLOCK = 512
BLOCKS_PER_SUBFILE = 32
SUBFILES = 8
FILES = 8
TRACKS = 8
BLOCKS_PER_TRACK = SUBFILES * BLOCKS_PER_SUBFILE     # one file/track pair
BLOCKS_PER_FILE = TRACKS * BLOCKS_PER_TRACK
BLOCKS_TOTAL = FILES * BLOCKS_PER_FILE

STAGING_FILL = 0xC6C6            # the MMB's INIT=C6C6 buffer

# Which phase's ZCON/QCON pool each phase's own cells follow on from.
POOL_PARENT = mmbstamp.POOL_PARENT


#
# The volume file
#
# Sparse: a header, a directory of block indices, then one 512-halfword
# block per directory entry, big-endian.  ext/sim/mmu/volume.coffee
# documents the layout.
#

MAGIC = b"MMUVOL01"
HEADER_SIZE = 32
FLAG_WRITE_PROTECT = 0x1


class Volume:
    def __init__(self, hwPerBlock: int = HW_PER_BLOCK, writeProtect: bool = False):
        self.hwPerBlock = hwPerBlock
        self.writeProtect = writeProtect
        self.blocks: dict[int, list[int]] = {}

    def write(self, index: int, words) -> None:
        if not 0 <= index < BLOCKS_TOTAL:
            raise ValueError(f"block index {index} off the end of the tape")
        b = list(words[: self.hwPerBlock])
        b += [0] * (self.hwPerBlock - len(b))
        self.blocks[index] = b

    def save(self, path: Path) -> int:
        idx = sorted(self.blocks)
        buf = bytearray(HEADER_SIZE + len(idx) * 4 + len(idx) * self.hwPerBlock * 2)
        buf[0:8] = MAGIC
        struct.pack_into(">III", buf, 8, self.hwPerBlock, len(idx),
                         FLAG_WRITE_PROTECT if self.writeProtect else 0)
        dataAt = HEADER_SIZE + len(idx) * 4
        for i, n in enumerate(idx):
            struct.pack_into(">I", buf, HEADER_SIZE + i * 4, n)
            at = dataAt + i * self.hwPerBlock * 2
            for j, w in enumerate(self.blocks[n]):
                struct.pack_into(">H", buf, at + j * 2, w & 0xFFFF)
        path.write_bytes(bytes(buf))
        return len(buf)


#
# Addressing
#

def blockIndex(track: int, file: int, subfile: int, block: int) -> int:
    """A block's ordinal position on the tape -- which is the tape address
    halfword itself: file, then track, then subfile, then block, the same
    order and the same field widths, so `blockIndex(*fromMm16(mm)) == mm`.
    """
    return (((file * TRACKS + track) * SUBFILES + subfile)
            * BLOCKS_PER_SUBFILE) + block


def fromMm16(mm: int) -> tuple[int, int, int, int]:
    """mmbstamp.mm16's packed form -> (track, file, subfile, block).  The
    packed halfword puts file above track; this returns them the other way
    round, the order a place on the tape is named in."""
    return ((mm >> 8) & 7, (mm >> 11) & 7, (mm >> 5) & 7, mm & 0x1F)


def blockAddr(index: int) -> tuple[int, int, int, int]:
    """The inverse of blockIndex."""
    return (index // BLOCKS_PER_TRACK % TRACKS,
            index // BLOCKS_PER_FILE % FILES,
            index // BLOCKS_PER_SUBFILE % SUBFILES,
            index % BLOCKS_PER_SUBFILE)


def fmtAddr(track: int, file: int, subfile: int, block: int) -> str:
    return f"{track}/{file}/{subfile}/{block}"


def fmtCard(track: int, file: int, subfile: int, block: int) -> str:
    """The five-digit form the ALLOC cards are written in."""
    return f"{file}{track}{subfile}{block:02d}"


#
# The tape record of one phase
#

def phaseRecord(mmuRoot: Path, deck: ConcardDeck, phase: int,
                ipl: set[int], mc: set[int], mmAddr: int = 0,
                budget: int | None = None) -> list[int]:
    """The halfwords the Mass Memory Build would write for this phase:
    its load blocks back to back, staging fill in every halfword the
    phase does not supply, and each block's closing checksum."""
    libPath = mmuRoot / f"PHASE{phase:02d}.lib"
    symPath = mmuRoot / f"PHASE{phase:02d}" / f"PHASE{phase:02d}.sym.json"
    lib = LibModule.read(libPath)
    sym = json.loads(symPath.read_text())
    root = f"OFTMP@{phase}"

    look = mmbstamp.protection_lookup(sym, mmbstamp.deck_protection(deck, root))
    fill = mmbstamp.fill_rule(sym, deck, root,
                              mmbstamp.map_extents(mmuRoot, deck, root))
    parentPool = mmbstamp.pool_own_start(mmuRoot, phase)
    # The same partition the phase table is built from, RESERVE regions
    # included.
    reserves = mmbstamp.reserve_regions(
        sym, mmbstamp.deck_reserved(deck, root))
    lbs = mmbstamp.derive_load_blocks(lib, parentPool, look,
                                      himem=phase in ipl, fill=fill,
                                      mc=phase in mc, reserves=reserves)

    # The same trim and packing the in-core phase table is stamped from;
    # the reader takes both from the table.  Called even with no budget,
    # because packing is what sets `sot`.
    mmbstamp.fit_budget(lbs, mmAddr, budget)

    # The phase's text by halfword address.  An auto-generated stack has
    # no tape text, so the staging fill covers it.
    text: dict[int, int] = {}
    for x in lib.extents:
        base = x.address // 2
        for i in range(x.hwLength):
            text[base + i] = (x.data[2 * i] << 8) | x.data[2 * i + 1]
    for a, b in mmbstamp.stack_holes(sym):
        for h in range(a, b):
            text.pop(h, None)

    # FCMCKSUM lies inside a load block, so it goes in before the blocks
    # are cut.
    for h, v in mmbstamp.ssl_checksum_slots(text, sym):
        text[h] = v

    out: list[int] = []
    cur = mmAddr                              # MM block, low byte = block in track
    for lb in lbs:
        if lb.reserve:                        # allocation only: no record
            continue
        if lb.sot:                            # pushed to the next track
            skip = mmbstamp.TRACK_BLOCKS - (cur & 0xFF)
            out.extend([STAGING_FILL] * (skip * HW_PER_BLOCK))
            cur = (cur & ~0xFF) + 0x100
        cur += -(-lb.length // HW_PER_BLOCK)
        slot = lb.length - 2                  # offset of the checksum slot
        block = [text.get(lb.start + i, STAGING_FILL) for i in range(lb.length)]
        total = 0
        for i in range(slot):
            total = (total + block[i]) & 0xFFFF
        block[slot] = 0x0000
        block[slot + 1] = total
        out.extend(block)
        # A load block is a whole number of tape blocks, and starts on one.
        while len(out) % HW_PER_BLOCK:
            out.append(STAGING_FILL)
    return out


#
# The rest of the tape
#

_ALLOC_RE = mmbstamp._ALLOC_RE
_MMDIR_RE = re.compile(r"\bMMDIR=(\d{5})")


def otherAllocations(con80: Path, area: int,
                     phaseMembers: set[str]) -> list[dict]:
    """Every allocation in this area that is NOT a phase: the mass memory
    directory, the transaction and message tables, checkpoint areas, etc."""
    out: list[dict] = []
    text = (con80 / f"MMUDAT{area}").read_text(errors="replace")
    for ln in text.splitlines():
        m = _ALLOC_RE.match(ln)
        if not m:
            continue
        member, addr, blks = m.group(1), m.group(2), m.group(3)
        if member in phaseMembers:
            continue
        t, f, sf, b = fromMm16(mmbstamp.mm16(addr))
        out.append({"member": member, "card": addr,
                    "address": fmtAddr(t, f, sf, b),
                    "allocBlocks": int(blks) if blks else None})
    return out


def loadmodRecord(path: Path, blocks: int,
                  halfwords: int | None) -> list[int]:
    """A LOADMOD member's tape record: the image over an INIT=C6C6 buffer,
    closing on the same (0000, sum16) checksum a phase's load block does.

    HWDS halfwords long when the SYSTEM card gives one; the rest of the
    allocation stays fill.
    """
    data = path.read_bytes()
    if len(data) % 2:
        raise ValueError(f"{path}: odd byte count, not whole halfwords")
    words = [int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data), 2)]
    # Room for the slot, on a fullword: an odd-length image pads to even
    # first.
    need = len(words) + (len(words) & 1) + 2
    length = max(halfwords or 0, need)
    if length > blocks * HW_PER_BLOCK:
        raise ValueError(f"{path}: record of {length} halfwords does not fit "
                         f"{blocks} blocks")
    rec = words + [STAGING_FILL] * (length - len(words))
    slot = length - 2
    total = 0
    for i in range(slot):
        total = (total + rec[i]) & 0xFFFF
    rec[slot] = 0x0000
    rec[slot + 1] = total
    rec += [STAGING_FILL] * (blocks * HW_PER_BLOCK - len(rec))
    return rec


#
# The IPL bootstrap copy
#

SECTOR_HW = 0x8000                        # halfwords a BSR/DSR sector spans
BOOT_PHASE = 1                            # PHASE01: FCMBOOT + the SSL's PSA
_COPY_IPL_RE = re.compile(r"^(\S+)\s+COPY,IPL\s*;")


def iplCopy(con80: Path, area: int) -> dict | None:
    """The bootstrap copy's allocation, or None if this area has none.

    `FMAIPL2 COPY,IPL;` on MMUSYSn names the member; its ALLOC card on
    MMUDATn places it.  Area 1 carries the only one.
    """
    member = None
    for ln in (con80 / f"MMUSYS{area}").read_text(errors="replace").splitlines():
        m = _COPY_IPL_RE.match(ln)
        if m:
            member = m.group(1)
            break
    if member is None:
        return None
    for ln in (con80 / f"MMUDAT{area}").read_text(errors="replace").splitlines():
        m = _ALLOC_RE.match(ln)
        if m and m.group(1) == member:
            t, f, sf, b = fromMm16(mmbstamp.mm16(m.group(2)))
            return {"member": member, "card": m.group(2),
                    "address": fmtAddr(t, f, sf, b),
                    "allocBlocks": int(m.group(3)) if m.group(3) else None}
    log.warning("%s COPY,IPL has no ALLOC card in MMUDAT%d", member, area)
    return None


def bootRecord(mmuRoot: Path, con80: Path) -> list[int]:
    """The IPL bootstrap copy: sector zero as the microcode loader leaves it.

    A flat record, one sector, PHASE01's linkedit text over the ALLOC
    card's INIT=C6C6 staging fill.  The reader is IOP microcode with no
    load-block machinery: it enters the PSW at halfword 0x14 of the PSA the
    record carries.  The IPL phase table is stamped into FCMPTAD1/2/3,
    which read X'FFFF' unstamped.
    """
    from ap101Utils import mmbstamp as mb
    libPath = mmuRoot / f"PHASE{BOOT_PHASE:02d}.lib"
    symPath = (mmuRoot / f"PHASE{BOOT_PHASE:02d}"
               / f"PHASE{BOOT_PHASE:02d}.sym.json")
    lib = LibModule.read(libPath)
    sym = json.loads(symPath.read_text())

    rec = [STAGING_FILL] * SECTOR_HW
    for x in lib.extents:
        base = x.address // 2
        if base + x.hwLength > SECTOR_HW:
            raise ValueError(f"{libPath}: extent at {base:#07x} runs past "
                             f"sector zero, which the bootstrap must fit")
        for i in range(x.hwLength):
            rec[base + i] = (x.data[2 * i] << 8) | x.data[2 * i + 1]

    sslpt, notes = mb.generate_sslpt(mmuRoot, con80)
    for n in notes:
        log.debug("sslpt: %s", n)
    image = mb.stamp_boot(
        b"".join(w.to_bytes(2, "big") for w in rec), sym, sslpt)
    return [int.from_bytes(image[i:i + 2], "big")
            for i in range(0, len(image), 2)]


def placeIplCopy(con80: Path, mmuRoot: Path, area: int,
                 vol: Volume) -> dict | None:
    """Write the bootstrap copy into its allocation.  Returns the row the
    report prints, or None when the area declares no COPY,IPL member."""
    row = iplCopy(con80, area)
    if row is None:
        return None
    try:
        words = bootRecord(mmuRoot, con80)
    except Exception as e:                               # noqa: BLE001
        row["status"] = f"failed: {e}"
        log.warning("%s: %s", row["member"], e)
        return row
    used = len(words) // HW_PER_BLOCK
    row["blocks"] = used
    row["halfwords"] = len(words)
    avail = row["allocBlocks"]
    if avail is not None and used > avail:
        row["status"] = f"OVERSIZE {used} > {avail} allocated"
        log.warning("%s needs %d blocks, %s allocates %d",
                    row["member"], used, row["card"], avail)
        return row
    t, f, sf, b = fromMm16(mmbstamp.mm16(row["card"]))
    first = blockIndex(t, f, sf, b)
    row["status"] = "written"
    clash = sorted(set(vol.blocks) & set(range(first, first + used)))
    if clash:
        a = blockAddr(clash[0])
        row["status"] += (f", OVERLAPS {len(clash)} block(s) from "
                          f"{fmtAddr(*a)}")
        log.warning("%s overlaps %d block(s) already written, from %s",
                    row["member"], len(clash), fmtAddr(*a))
    for i in range(used):
        vol.write(first + i, words[i * HW_PER_BLOCK:(i + 1) * HW_PER_BLOCK])
    return row


def placeLoadmods(con80: Path, images: dict[str, Path],
                  vol: Volume) -> list[dict]:
    """Write each supplied LOADMOD member into every allocation that
    carries it.  Returns a row per allocation, supplied or not, so the
    report lists the empty ones."""
    from tools.mmubuild import MMUDeck, build as mmuTree

    rows: list[dict] = []
    try:
        placements = mmuTree(MMUDeck(con80)).loadmod_allocations()
    except Exception as e:                               # noqa: BLE001
        log.warning("LOADMOD allocations unavailable: %s", e)
        return rows

    for pl in placements:
        t, f, sf, b = fromMm16(mmbstamp.mm16(pl.addr))
        row = {
            "member": pl.member, "label": pl.label, "sysid": pl.sysid,
            "card": pl.addr, "address": fmtAddr(t, f, sf, b),
            "allocBlocks": pl.blocks, "halfwords": pl.halfwords,
        }
        rows.append(row)
        src = images.get(pl.member)
        if src is None:
            row["status"] = "not supplied"
            continue
        try:
            words = loadmodRecord(src, pl.blocks, pl.halfwords)
        except Exception as e:                           # noqa: BLE001
            row["status"] = f"failed: {e}"
            log.warning("%s: %s", pl.member, e)
            continue
        n = (src.stat().st_size + 1) // 2
        row["blocks"] = pl.blocks
        row["imageHalfwords"] = n
        if n > pl.blocks * HW_PER_BLOCK:
            row["status"] = (f"OVERSIZE {n} halfwords > "
                             f"{pl.blocks * HW_PER_BLOCK} allocated")
            log.warning("%s needs %d halfwords, %s allocates %d",
                        pl.member, n, pl.addr, pl.blocks * HW_PER_BLOCK)
            continue
        # HWDS is the record's length: the image plus its pad and
        # checksum slot.
        need = n + (n & 1) + 2
        if pl.halfwords is not None and need > pl.halfwords:
            log.warning("%s needs %d halfwords with its checksum, %s declares "
                        "HWDS=%d", pl.member, need, pl.label, pl.halfwords)
        first = blockIndex(t, f, sf, b)
        clash = sorted(set(vol.blocks) & set(range(first, first + pl.blocks)))
        row["status"] = "written"
        if clash:
            a = blockAddr(clash[0])
            row["status"] += (f", OVERLAPS {len(clash)} block(s) "
                              f"from {fmtAddr(*a)}")
            log.warning("%s overlaps %d block(s) already written, from %s",
                        pl.member, len(clash), fmtAddr(*a))
        for i in range(pl.blocks):
            vol.write(first + i,
                      words[i * HW_PER_BLOCK:(i + 1) * HW_PER_BLOCK])
    return rows


def directoryAddress(con80: Path) -> str | None:
    """MMDIR on the master IPL card: where a GPC looks for the tape directory.
    """
    m = _MMDIR_RE.search((con80 / "MMLOAD").read_text(errors="replace"))
    return m.group(1) if m else None


#
# Driver
#

def buildVolume(con80: Path, mmuRoot: Path, area: int, vol: Volume,
                loadmods: dict[str, Path] | None = None
                ) -> tuple[list[dict], list[dict], str | None, list[dict],
                           dict | None]:
    """Lay every allocated phase onto `vol`, then the IPL bootstrap copy,
    then every supplied LOADMOD member.  Returns (per-phase rows, the
    area's non-phase allocations, the directory's tape address,
    per-LOADMOD-allocation rows, the bootstrap copy's row)."""
    deck = ConcardDeck(con80)
    table = mmbstamp.parse_area(con80, area)
    ipl = mmbstamp.ipl_phases(con80)

    rows: list[dict] = []
    phaseMembers: set[str] = set()
    for ln in (con80 / f"MMUSYS{area}").read_text(errors="replace").splitlines():
        m = re.match(r"^(\S+)\s+PHASE,", ln)
        if m:
            phaseMembers.add(m.group(1))
    for phase in sorted(table.mm_of_phase):
        mm = table.mm_of_phase[phase]
        t, f, s, b = fromMm16(mm)
        row = {
            "phase": phase,
            "address": fmtAddr(t, f, s, b),
            "card": fmtCard(t, f, s, b),
            "allocBlocks": table.blks_of_phase.get(phase),
            "mc": phase in table.mc_phases,
            "ipl": phase in ipl,
        }
        rows.append(row)

        libPath = mmuRoot / f"PHASE{phase:02d}.lib"
        if not libPath.exists():
            row["status"] = "not built"
            continue
        try:
            words = phaseRecord(mmuRoot, deck, phase, ipl, table.mc_phases,
                                mm, table.blks_of_phase.get(phase))
        except Exception as e:                       # noqa: BLE001
            row["status"] = f"failed: {e}"
            log.warning("phase %d: %s", phase, e)
            continue

        used = len(words) // HW_PER_BLOCK
        row["blocks"] = used
        row["halfwords"] = len(words)
        avail = table.blks_of_phase.get(phase)
        if avail is not None and used > avail:
            row["status"] = f"OVERSIZE {used} > {avail} allocated"
            log.warning("phase %d needs %d blocks, %s allocates %d",
                        phase, used, row["card"], avail)
        else:
            row["status"] = "written"

        first = blockIndex(t, f, s, b)
        if first + used > BLOCKS_TOTAL:
            row["status"] = "off the end of the tape"
            continue
        clash = sorted(set(vol.blocks) & set(range(first, first + used)))
        if clash:
            a = blockAddr(clash[0])
            row["status"] = (f"{row['status']}, OVERLAPS {len(clash)} block(s) "
                             f"from {fmtAddr(*a)}")
            log.warning("phase %d overlaps %d block(s) already written, "
                        "from %s", phase, len(clash), fmtAddr(*a))
        for i in range(used):
            vol.write(first + i,
                      words[i * HW_PER_BLOCK:(i + 1) * HW_PER_BLOCK])
    iplRow = placeIplCopy(con80, mmuRoot, area, vol)
    lmRows = placeLoadmods(con80, loadmods or {}, vol)
    lmMembers = {r["label"] for r in lmRows}
    if iplRow is not None:
        lmMembers.add(iplRow["member"])
    others = [o for o in otherAllocations(con80, area, phaseMembers)
              if o["member"] not in lmMembers]
    return (rows, others, directoryAddress(con80), lmRows, iplRow)


def report(rows: list[dict], others: list[dict], mmdir: str | None,
           lmRows: list[dict] | None = None, iplRow: dict | None = None,
           out=sys.stdout) -> None:
    print(f"{'phase':>5}  {'card':<5}  {'t/f/s/b':<10}  {'blocks':>6}"
          f"  {'alloc':>5}  flags  status", file=out)
    for r in rows:
        flags = "".join(("M" if r["mc"] else "-", "I" if r["ipl"] else "-"))
        print(f"{r['phase']:>5}  {r['card']:<5}  {r['address']:<10}"
              f"  {r.get('blocks', ''):>6}  {r.get('allocBlocks', ''):>5}"
              f"  {flags:<5}  {r['status']}", file=out)
    placed = [r for r in rows if "blocks" in r]
    clean = [r for r in placed if r["status"] == "written"]
    blocks = sum(r["blocks"] for r in placed)
    print(f"\n{len(placed)} of {len(rows)} allocated phases written, "
          f"{blocks} tape blocks ({blocks * HW_PER_BLOCK} halfwords)", file=out)
    if len(clean) != len(placed):
        print(f"{len(placed) - len(clean)} of them do not fit their "
              f"allocation -- see the status column", file=out)

    if iplRow:
        print(f"\n{'member':<9}  {'card':<5}  {'t/f/s/b':<10}  "
              f"{'blocks':>6}  {'alloc':>5}  status", file=out)
        print(f"  {iplRow['member']:<7}  {iplRow['card']:<5}  "
              f"{iplRow['address']:<10}  {iplRow.get('blocks', ''):>6}  "
              f"{str(iplRow['allocBlocks'] or ''):>5}  {iplRow['status']}",
              file=out)
        print("the IPL bootstrap copy: what the microcode loader reads into "
              "sector zero", file=out)

    if lmRows:
        print(f"\n{'member':<9}  {'function':<9}  {'card':<5}  "
              f"{'t/f/s/b':<10}  {'alloc':>5}  status", file=out)
        for r in lmRows:
            print(f"{r['member']:<9}  {r['label']:<9}  {r['card']:<5}  "
                  f"{r['address']:<10}  {r['allocBlocks']:>5}  {r['status']}",
                  file=out)
        placedLm = [r for r in lmRows if "blocks" in r]
        print(f"{len(placedLm)} of {len(lmRows)} load-module allocations "
              f"written", file=out)

    if not others:
        return
    print("\nnot generated -- the ground build's own data:", file=out)
    if mmdir:
        t, f, sf, b = fromMm16(mmbstamp.mm16(mmdir))
        print(f"  {mmdir:<5}  {fmtAddr(t, f, sf, b):<10}  "
              f"{'':>5}         the mass memory DIRECTORY (MMDIR)", file=out)
    for r in others:
        print(f"  {r['card']:<5}  {r['address']:<10}  "
              f"{str(r['allocBlocks'] or ''):>5} blocks  {r['member']}",
              file=out)
    print("\nA GPC reads the directory to find everything else, so a volume "
          "from here\nserves the phases but nothing that points at them.",
          file=out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="mmu2mmv",
        description="Write the built phases onto a mass memory tape volume")
    ap.add_argument("--con80", default="code/OI340700/CON80", type=Path,
                    help="linkedit/MMU card deck (default: "
                         "code/OI340700/CON80)")
    ap.add_argument("--mmu", default="build/mmu", type=Path,
                    help="con80build output root holding PHASEnn.lib "
                         "(default: build/mmu)")
    ap.add_argument("--area", type=int, default=1,
                    help="PASS area whose MMUSYSn/MMUDATn cards to use; the "
                         "areas are redundant copies of the same software "
                         "(default: 1)")
    ap.add_argument("--out", type=Path,
                    help="volume file to write (default: <mmu>/mmu.mmv)")
    ap.add_argument("--manifest", type=Path,
                    help="also write the tape map as JSON")
    ap.add_argument("--report", action="store_true",
                    help="print the tape map and do not write a volume")
    ap.add_argument("--loadmod", action="append", default=[],
                    metavar="NAME=FILE",
                    help="a LOADMOD member's image, as raw big-endian "
                         "halfwords, written into every allocation that "
                         "carries it (e.g. DEUCFLM=build/…/DEUCFLM.bin); "
                         "repeatable")
    ap.add_argument("--write-protect", action="store_true",
                    help="mark the volume write protected")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(levelname)s: %(message)s")

    if not a.con80.is_dir():
        ap.error(f"{a.con80}: card deck not found")

    images: dict[str, Path] = {}
    for spec in a.loadmod:
        name, _, path = spec.partition("=")
        if not path:
            ap.error(f"--loadmod {spec}: expected NAME=FILE")
        p = Path(path)
        if not p.is_file():
            ap.error(f"--loadmod {spec}: {p} not found")
        images[name.upper()] = p

    vol = Volume(writeProtect=a.write_protect)
    rows, others, mmdir, lmRows, iplRow = buildVolume(a.con80, a.mmu, a.area,
                                                     vol, images)
    report(rows, others, mmdir, lmRows, iplRow)

    if a.manifest:
        a.manifest.write_text(json.dumps(
            {"area": a.area, "con80": str(a.con80), "mmu": str(a.mmu),
             "phases": rows, "loadmods": lmRows, "iplCopy": iplRow},
            indent=2) + "\n")
        log.info("manifest -> %s", a.manifest)

    if a.report:
        return 0
    out = a.out or (a.mmu / "mmu.mmv")
    n = vol.save(out)
    log.info("%s: %d blocks, %d bytes", out, len(vol.blocks), n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
