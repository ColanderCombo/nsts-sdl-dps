#!/usr/bin/env python3
"""mmu2mmv -- write the built phases onto a mass memory tape volume (.mmv).

The MMU load-build cards say where everything goes.  An ALLOC card gives a
member's tape address as five decimal digits FTSBB -- file, track,
subfile, and a two-digit block -- and how many 512-halfword blocks it may
occupy; a PHASE card says which memory-configuration phase that member
is.  Joining the two gives phase -> tape address, which is what
`ap101Utils.mmbstamp.parse_area` already builds for the in-core phase
tables.  This takes the same map, finds each phase's linked load module
under the con80build output, and lays its tape record into its
allocation.

The tape record of a phase is its LOAD BLOCKS, back to back:
`mmbstamp.derive_load_blocks` partitions the phase's linkedit image into
them, each block running from its first halfword to a closing 2-halfword
checksum slot.  The Mass Memory Build stages a block from an INIT=C6C6
buffer, so every halfword of the block the phase does not itself supply
-- alignment pad, the gap the fill rule opens, an allocation-only csect
-- is C6C6, and the slot carries (0000, sum16 of the block before it).
That is the same content model `mmu2fcm --stamp-checksums` reads back
when it emulates the load, so a block written here and loaded there round
trips.

What this does NOT generate: the mass memory DIRECTORY, the transaction
and message tables, and the IPL phase table -- the ground build's own
data structures, which live in their own allocations and are what a GPC
consults to find any of this.  A volume from here therefore has the
phases at the right addresses and nothing to tell a GPC they are
there.  `--report` lists every allocation and says which of them
were filled.

The `DIRECTRY` cards that declare those tables are read and rendered by
`tools.mmubuild` (`--directories`, `--image`); what is still missing here
is writing the result -- into the tape image for a free-standing one, and
into the owning phase's load module for a `DMMD=YES` one, which belongs
beside the other stamped tables in `ap101Utils.mmbstamp`.

Usage:
  mmu2mmv --con80 code/OI340700/CON80 --mmu build/mmu --out tape.mmv
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
# Sparse, because a full tape is 16 MB and a real one is mostly blank:
# a header, a directory of block indices, then one 512-halfword block per
# directory entry, big-endian throughout.  ext/sim/mmu/volume.coffee
# reads and writes the same thing and documents the layout.
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
                ipl: set[int], mc: set[int]) -> list[int]:
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
    parent = POOL_PARENT.get(phase, 2)
    parentPool = mmbstamp.pool_next_hw(
        LibModule.read(mmuRoot / f"PHASE{parent:02d}.lib"))
    lbs = mmbstamp.derive_load_blocks(lib, parentPool, look,
                                      himem=phase in ipl, fill=fill,
                                      mc=phase in mc)

    # The phase's own text, by halfword address.  An auto-generated stack
    # is allocation with no tape text, so the staging fill covers it --
    # the same cut mmu2fcm makes when it reads these blocks back.
    text: dict[int, int] = {}
    for x in lib.extents:
        base = x.address // 2
        for i in range(x.hwLength):
            text[base + i] = (x.data[2 * i] << 8) | x.data[2 * i + 1]
    for a, b in mmbstamp.stack_holes(sym):
        for h in range(a, b):
            text.pop(h, None)

    out: list[int] = []
    for lb in lbs:
        slot = lb.length - 2                  # offset of the checksum slot
        block = [text.get(lb.start + i, STAGING_FILL) for i in range(lb.length)]
        total = 0
        for i in range(slot):
            total = (total + block[i]) & 0xFFFF
        block[slot] = 0x0000
        block[slot + 1] = total
        out.extend(block)
        # A load block is a whole number of tape blocks: the merge rule
        # that builds them caps a block at 16384 halfwords, which is 32
        # tape blocks exactly, so they are counted in tape blocks and each
        # starts on one.
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


def directoryAddress(con80: Path) -> str | None:
    """MMDIR on the master IPL card: where a GPC looks for the tape directory.
    """
    m = _MMDIR_RE.search((con80 / "MMLOAD").read_text(errors="replace"))
    return m.group(1) if m else None


#
# Driver
#

def buildVolume(con80: Path, mmuRoot: Path, area: int,
                vol: Volume) -> tuple[list[dict], list[dict], str | None]:
    """Lay every allocated phase onto `vol`.  Returns (per-phase rows, the
    area's non-phase allocations, the directory's tape address)."""
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
            words = phaseRecord(mmuRoot, deck, phase, ipl, table.mc_phases)
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
    return (rows, otherAllocations(con80, area, phaseMembers),
            directoryAddress(con80))


def report(rows: list[dict], others: list[dict], mmdir: str | None,
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
    ap.add_argument("--write-protect", action="store_true",
                    help="mark the volume write protected")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(levelname)s: %(message)s")

    if not a.con80.is_dir():
        ap.error(f"{a.con80}: card deck not found")

    vol = Volume(writeProtect=a.write_protect)
    rows, others, mmdir = buildVolume(a.con80, a.mmu, a.area, vol)
    report(rows, others, mmdir)

    if a.manifest:
        a.manifest.write_text(json.dumps(
            {"area": a.area, "con80": str(a.con80), "mmu": str(a.mmu),
             "phases": rows}, indent=2) + "\n")
        log.info("manifest -> %s", a.manifest)

    if a.report:
        return 0
    out = a.out or (a.mmu / "mmu.mmv")
    n = vol.save(out)
    log.info("%s: %d blocks, %d bytes", out, len(vol.blocks), n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
