#!/usr/bin/env python3
"""g3dat -- generate the mass-memory-build-stamped G3 archive.

`FCMG3DAT` is the G3 archive destination table:

Generation rules:
    - The G3 overlay lives in PHASE06 of the build and the archive
      process uses this table to control saving & restoring the
      data to a location in high memory.
    - Entries in the table correspond to PHASE06 load blocks.
    - The load block's descriptor is written to the table with
      the flags shifted right 4 bits.
    - If the load block would cross a 32K sector boundary it's
      split
    - The load blocks are written stacked downwards starting right
      below the top of the region reserved for the archive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ap101Utils import mmbstamp
from ap101Utils.libModule import EsdType, LibModule

G3DAT_CSECT = "FCMG3DAT"
G3DAT_SIZE = 161                 # hw: 50*3 table + 11-hw trailer
TABLE_HW = 150                   # FCMDNMBR(50) * 3
MAX_ENTRIES = 50                 # FCMDNMBR

G3_PHASE = 6                     # the G3 program overlay
ARCHIVE_SECTOR = 15              # target sector for archive
SECTOR_HW = 0x8000               # sector size
WINDOW = 0x8000                  # address bit for a non-zero sector

# Placed top-down in the archive sector, in move order.
MODULES = ("FCMG3DAT", "FCMG3RTV", "FCMG3STR")

DSR_FORM = 0x000F                # ZCON sector as a DSR (data pointer)
BSR_FORM = 0x00F0                # ZCON sector as a BSR (code pointer)


class G3DatError(mmbstamp.StampError):
    pass


@dataclass
class G3Dat:
    words: list[int]                              # G3DAT_SIZE halfwords
    entries: list[tuple[int, int, int]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def module_extents(mmu_root: Path) -> dict[str, int]:
    """{module: length in hw} for the three upper-memory modules, from the
    CESD SD entries of whichever phase links them (OPS0 => phase 2).

    The SD length is the moved length -- we copy each module
    through its ENTRY label, which is the last halfword of the csect."""
    want = set(MODULES)
    for lib_path in sorted(Path(mmu_root).glob("PHASE*.lib")):
        found = {}
        for name, type_, _addr, length in LibModule.read(lib_path).symbols():
            # SD only: cross-phase resolution leaves ER/LD entries for these
            # names in other phases' CESDs, and those carry no real length.
            if name in want and type_ == EsdType.SD and length:
                found[name] = length // 2
        if len(found) == len(want):
            return found
        if found:
            raise G3DatError(
                f"{lib_path.name} defines only {sorted(found)} of "
                f"{sorted(want)} -- the G3 archive modules must link "
                f"together")
    raise G3DatError(f"none of {sorted(want)} found in {mmu_root}/PHASE*.lib "
                     f"-- is the phase carrying deck OPS0 built?")


def place_modules(lengths: dict[str, int]) -> dict[str, int]:
    at, cur = {}, SECTOR_HW
    for name in MODULES:
        cur -= lengths[name]
        if cur < 0:
            raise G3DatError(f"the G3 archive modules do not fit in one "
                             f"sector ({name} underruns it)")
        at[name] = cur | WINDOW
    return at


def split_for_destination(lbs, top_hw: int):
    """Cut each LB where the archive -- filling downward from `top_hw`
    (exclusive, absolute) -- would cross a 32K sector boundary."""
    out, dest = [], top_hw
    for lb in lbs:
        addr, flags, length = lb.words()
        while length:
            room = dest - ((dest - 1) & ~(SECTOR_HW - 1))
            take = min(length, room)
            out.append((addr & 0xFFFF, flags >> 4, take))
            dest -= take
            addr += take
            length -= take
    if dest < 0:
        raise G3DatError(f"the G3 archive underruns memory at hw {dest:#x}")
    return out, dest


def generate(mmu_root: Path, con80: Path, phase: int = G3_PHASE) -> G3Dat:
    mmu_root = Path(mmu_root)
    src = mmbstamp.load_phase_source(con80)
    if phase not in src.assigned:
        raise G3DatError(f"phase {phase} is not MMUSYS-assigned -- it has no "
                         f"load blocks to archive")
    lbs, mm, _ncont, _crossed = mmbstamp.phase_load_blocks(mmu_root, phase,
                                                           src)

    lengths = module_extents(mmu_root)
    at = place_modules(lengths)
    base = ARCHIVE_SECTOR * SECTOR_HW
    top = base + (at["FCMG3STR"] & (SECTOR_HW - 1))       # exclusive
    entries, bottom = split_for_destination(lbs, top)
    if len(entries) > MAX_ENTRIES:
        raise G3DatError(f"{len(entries)} DAT entries exceed the "
                         f"{MAX_ENTRIES}-entry table (FCMDNMBR)")
    total = sum(e[2] for e in entries)

    words = [0] * G3DAT_SIZE
    for i, (addr, flags, length) in enumerate(entries):
        words[3 * i:3 * i + 3] = [addr, flags, length]
    words[TABLE_HW:] = [
        3 * len(entries),                                 # FCMDTSIZ
        total >> 16, total & 0xFFFF,                      # FCMD3SIZ
        at["FCMG3DAT"], DSR_FORM,                         # FCMDPDAT
        at["FCMG3RTV"], BSR_FORM,                         # FCMDPRTV
        at["FCMG3STR"], BSR_FORM,                         # FCMDPSTR
        (at["FCMG3STR"] - 1) & 0xFFFF, DSR_FORM,          # FCMDPG3O/FCMDATLG
    ]

    notes = [f"phase {phase}: {len(lbs)} load blocks (mm {mm:04X}) -> "
             f"{len(entries)} DAT entries, {total} hw archived",
             f"archive sector {ARCHIVE_SECTOR}: "
             + ", ".join(f"{n} @ {at[n]:04X} ({lengths[n]} hw)"
                         for n in MODULES),
             f"archive spans {bottom:#07x}..{top - 1:#07x} (absolute hw)"]
    return G3Dat(words, entries, notes)


def stamp(image: bytes, sym: dict, dat: G3Dat) -> bytes:
    """Overlay the generated table at the target address."""
    sec = {s["name"]: s for s in sym.get("sections", [])}.get(G3DAT_CSECT)
    if sec is None:
        raise G3DatError(f"{G3DAT_CSECT} not present in the composed image")
    if sec["size"] < G3DAT_SIZE:
        raise G3DatError(f"{G3DAT_CSECT} is {sec['size']} hw, expected "
                         f">= {G3DAT_SIZE}")
    buf = bytearray(image)
    off = sec["address"] * 2
    buf[off:off + 2 * G3DAT_SIZE] = b"".join(
        w.to_bytes(2, "big") for w in dat.words)
    return bytes(buf)
