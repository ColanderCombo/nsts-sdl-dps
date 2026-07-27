#!/usr/bin/env python3
"""mmbstamp -- generate the Mass-Memory-Build-stamped phase tables.

The mass memory build process generates two CSECTS containing tables
with MM content information:

  #PFCMGPT (1,093 hw)  in-core phase table: 16 4-hw phase descriptors
                       (phases 3..18) + packed 3-hw load-block descriptors
  #PCDCPHA (57 hw)     alternate-MM-area phase table: CDCV_PHASES(19,3),
                       the STARTING_MM_ADD of each phase in PASS areas 1-3

Both csects read as INITIAL values (zeros / -1) in the linkedit load
module.  Stamping the generated tables is opt-in (--stamp-phase-tables)
so comparisons against the load module stay clean by default.

Generation rules:

  * Load blocks = contiguous checksum-group runs of the phase's linkedit
    image.  Our .lib TEXT extents are those groups (each is followed by
    its 2-hw fullword-aligned checksum slot, which the LB INCLUDES:
    lb_len = roundup_fullword(extent_end) - extent_start + 2).  Adjacent
    groups (next start == previous group end incl. slot) MERGE into one LB
    while total length stays <= 16384 hw (32 MMU blocks) and bank and
    block protection agree; a single group may exceed 16384 and then
    stands alone.
  * LB protection = the CON80 deck's per-OVERLAY SET/CLEAR block state
    (class default for unmarked blocks), majority-by-halfword over the
    run -- NOT the per-halfword store-protect bitmap: the flight flag
    tracks the LE block state, and small embedded exceptions (#Y patch
    csects, per-csect PROT-card holes) do not split or flip an LB.
  * The Z1 ZCON pool is one LB per phase: [own pool start, zconPoolNext)
    where zconPoolNext (the .lib LSTA cursor, which already includes the
    pool's closing checksum slot) comes from the phase and the pool start
    from its first pool extent at/above the PARENT phase's cursor;
    re-supplies of parent-phase pool cells below that are not tape
    payload and are dropped.
  * Upper-memory (sector >= 8) extents are listed only for IPL-set phases
    (MMLOAD IPL,PH=(...)): the OPS-overlay BCE builder ignores HIMEM LBs
    and only the SSL loads upper memory at IPL.
  * MM packing: starting at the phase's ALLOC address (MMUDATn decks,
    ADDR=FTSBB -> mm16 = F<<11|T<<8|S<<5|BB; +5 blocks for PHTAB=YES
    phases -- the on-MMU phase-table copy precedes the phase data), each
    LB takes ceil(len/512) 512-hw blocks, back to back; an LB may not
    cross a track (256-block) boundary: it skips to the next track and
    carries the SOT flag.  NUM_CONT_MM_BLKS counts data blocks plus
    track-skip waste; bit0 (0x8000) = the span crossed a track boundary.
  * Unassigned phases (no MMUSYS PHASE card: 11, 17) get the placeholder
    the MMB emits: one LB descriptor (8000, 0610, 0004), mm addr 0,
    ncont 1.
  * #PCDCPHA rows: phases 3..18 = the per-area mm16 (+PHTAB offset);
    unassigned phases 0; rows 19-21 = C6C6 (the MMB staged the 57-hw
    store from an INIT=C6C6-filled buffer that extends past phase 18).
"""
from __future__ import annotations

import bisect
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ap101Utils.concard import ConcardDeck, layout_program
from ap101Utils.conlayout import default_protected as _class_default
from ap101Utils.libModule import LibModule

GPT_SIZE = 1093                  # #PFCMGPT halfwords (64 + 3*343)
CPHA_SIZE = 57                   # #PCDCPHA halfwords (19 rows x 3 areas)
GPT_CSECT = "#PFCMGPT"
CPHA_CSECT = "#PCDCPHA"

BANK_HW = 0x8000                 # halfwords per 32K bank (one DSR value)
LB_CAP_HW = 16384                # merge cap: 32 MMU blocks; single checksum
                                 # groups may exceed it
BLOCK_HW = 512                   # MMU block
TRACK_BLOCKS = 256               # blocks per track (track = +0x100 in mm16)
HIMEM_SECTOR = 8                 # FCMHIMEM: sector >= 8 = upper memory
FILL = 0xC6C6                    # MMB staging-buffer fill (INIT=C6C6)

# Placeholder LB descriptor the MMB emits for unassigned phases (11, 17):
# addr 8000, flags 0610, len 4.
PLACEHOLDER_LB = (0x8000, 0x0610, 4)

# Pool-cursor parent: the phase loaded before this one in its memory
# configuration (CZ2V_GRT_PHASES row order: MFB column first, program
# columns ascending; base = resident phase 2).  The Z1 ZCON pool is
# cursor-allocated along that chain, so a phase's own pool cells start at
# its parent's zconPoolNext.
POOL_PARENT = {3: 2, 4: 3, 5: 3, 6: 3, 7: 3, 8: 3, 9: 2, 10: 2, 12: 9,
               13: 2, 14: 2, 15: 14, 16: 14, 18: 8}


class StampError(Exception):
    pass


# ---------------------------------------------------------------------------
# CON80 side: MMUSYSn / MMUDATn / MMLOAD
# ---------------------------------------------------------------------------

_PHASE_RE = re.compile(r"^(\S+)\s+PHASE,(\S+)")
_ALLOC_RE = re.compile(r"^(\S+)\s+ALLOC,ADDR=(\d{5})")
_IPL_RE = re.compile(r"\bIPL,PH=\(([\d,]+)\)")


def mm16(addr: str) -> int:
    """CON80 ALLOC ADDR=FTSBB -> 16-bit MMU address (MMULDTBL bit layout:
    00|File(3)|Track(3)|Subfile(3)|Block(5); BB is DECIMAL 00-31)."""
    if len(addr) != 5 or not addr.isdigit():
        raise StampError(f"malformed ALLOC ADDR {addr!r} (want FTSBB)")
    f, t, s, bb = int(addr[0]), int(addr[1]), int(addr[2]), int(addr[3:5])
    if f > 7 or t > 7 or s > 7 or bb > 31:
        raise StampError(f"ALLOC ADDR {addr!r} field out of range")
    return (f << 11) | (t << 8) | (s << 5) | bb


@dataclass
class AreaTable:
    """One PASS area's phase directory: MMUSYSn PHASE cards joined with
    MMUDATn ALLOC addresses."""
    area: int
    mm_of_phase: dict = field(default_factory=dict)    # phase -> mm16
                                                       # (incl. PHTAB offset)


def parse_area(con80: Path, area: int) -> AreaTable:
    out = AreaTable(area)
    members: dict[str, tuple[int, bool]] = {}          # member -> (ph, phtab)
    for ln in (con80 / f"MMUSYS{area}").read_text(errors="replace").splitlines():
        m = _PHASE_RE.match(ln)
        if not m:
            continue
        opts = m.group(2).rstrip(";")
        ph = phtab = None
        for kv in opts.split(","):
            k, _, v = kv.partition("=")
            if k == "PH":
                ph = int(v)
            elif k == "PHTAB":
                phtab = v.startswith("Y")
        if ph is not None:
            members[m.group(1)] = (ph, bool(phtab))
    allocs: dict[str, int] = {}
    for ln in (con80 / f"MMUDAT{area}").read_text(errors="replace").splitlines():
        m = _ALLOC_RE.match(ln)
        if m:
            allocs[m.group(1)] = mm16(m.group(2))
    for member, (ph, phtab) in members.items():
        if member in allocs:
            out.mm_of_phase[ph] = allocs[member] + (5 if phtab else 0)
    return out


def ipl_phases(con80: Path) -> set[int]:
    """The IPL-loaded phase set from the MMLOAD card (IPL,PH=(...))."""
    for ln in (con80 / "MMLOAD").read_text(errors="replace").splitlines():
        m = _IPL_RE.search(ln)
        if m and not ln.lstrip().startswith("*"):
            return {int(p) for p in m.group(1).split(",")}
    raise StampError(f"{con80}/MMLOAD: no IPL,PH=(...) card")


# ---------------------------------------------------------------------------
# Protection: CON80 per-OVERLAY SET/CLEAR blocks + class defaults
# ---------------------------------------------------------------------------

def patch_aware_default(name: str) -> bool:
    """conlayout's class default, with #Y patch csects unprotected (they
    patch unprotected data regions)."""
    if name.startswith("#Y"):
        return False
    return _class_default(name)


def deck_protection(deck: ConcardDeck, root: str) -> dict[str, bool]:
    """Per-csect protection from the deck: a SET/CLEAR card marks the
    csects of the CURRENT OVERLAY block placed since the last mark;
    blocks with no mark default by name class.  An OVERLAY card finalizes
    the pending block (a SET in the next overlay does not reach back)."""
    prot: dict[str, bool] = {}
    pending: list[str] = []

    def finalize(mark=None):
        for n in pending:
            prot[n] = patch_aware_default(n) if mark is None else mark
        pending.clear()

    for op in layout_program(deck, root):
        v = op.verb
        if v in ("OVERLAY", "BANK", "PHASE"):
            finalize()
        elif v in ("INSERT", "RESERVE", "STACK"):
            pending.append(op.operand)
        elif v in ("SET", "CLEAR"):
            finalize(v == "SET")
    finalize()
    return prot


def protection_lookup(sym: dict, prot: dict[str, bool]):
    """hw -> protected?|None from the phase's sym.json sections and the
    deck protection map (fallback: name-class default)."""
    ivals = sorted(
        (s["address"], s["address"] + s["size"],
         prot.get(s["name"], patch_aware_default(s["name"])))
        for s in sym.get("sections", []))
    starts = [iv[0] for iv in ivals]

    def look(hw: int):
        i = bisect.bisect_right(starts, hw) - 1
        while i >= 0 and ivals[i][1] > hw - 0x2000:
            if ivals[i][0] <= hw < ivals[i][1]:
                return ivals[i][2]
            i -= 1
        return None

    return look


# ---------------------------------------------------------------------------
# Load-block derivation
# ---------------------------------------------------------------------------

def roundfw(hw: int) -> int:
    """Round a halfword address up to a fullword boundary."""
    return hw + (hw & 1)


def pool_next_hw(lib: LibModule) -> int:
    return lib.linkState.get("zconPoolNext", 0) // 2


@dataclass
class LoadBlock:
    start: int                   # effective hw address (sector*0x8000+off)
    length: int                  # hw, INCLUDING the 2-hw checksum tail
    protected: bool
    sot: bool = False            # start-of-track (set by MM packing)

    @property
    def sector(self) -> int:
        return self.start // BANK_HW

    def words(self) -> tuple[int, int, int]:
        addr = (self.start & 0x7FFF) | (0x8000 if self.sector else 0)
        flags = 0x0600 | (self.sector << 4)
        if self.protected:
            flags |= 0x8000
        if self.sot:
            flags |= 0x4000
        return addr, flags, self.length


def derive_load_blocks(lib: LibModule, parent_pool: int, look,
                       himem: bool) -> list[LoadBlock]:
    """The phase's LB partition from its .lib extents (rules in the module
    docstring)."""
    pool = pool_next_hw(lib)
    units: list[list] = []       # [start, end_incl_checksum, protected]
    pool_start = None
    for x in sorted(lib.extents, key=lambda x: x.address):
        s, e = x.address // 2, x.address // 2 + x.hwLength
        if e <= pool:                        # Z1 pool area extent
            if s >= parent_pool and pool_start is None:
                pool_start = s               # own cells begin here
            continue                         # (re-supplies dropped)
        if not himem and s >= HIMEM_SECTOR * BANK_HW:
            continue                         # only the SSL loads upper memory
        while s < e:                         # split at bank boundaries
            bnd = (s // BANK_HW + 1) * BANK_HW
            pe = min(e, bnd)
            votes = [v for v in (look(h) for h in range(s, pe))
                     if v is not None]
            maj = sum(votes) * 2 > len(votes) if votes else False
            units.append([s, min(roundfw(pe) + 2, bnd), maj])
            s = pe
    if pool_start is not None:
        units.insert(0, [pool_start, pool, True])   # pool: ZCON constants
    units.sort()

    merged: list[list] = []
    for u in units:
        if merged:
            c = merged[-1]
            if (u[0] == c[1] and u[2] == c[2]
                    and u[0] // BANK_HW == c[0] // BANK_HW
                    and u[1] - c[0] <= LB_CAP_HW):
                c[1] = u[1]
                continue
        merged.append(u)
    return [LoadBlock(s, e - s, p) for s, e, p in merged]


def pack_mm(lbs: list[LoadBlock], mm_start: int) -> tuple[int, bool]:
    """Assign MM blocks in table order from mm_start; sets each LB's SOT
    flag; returns (NUM_CONT_MM_BLKS, crossed_track)."""
    cur = mm_start
    for lb in lbs:
        blocks = -(-lb.length // BLOCK_HW)
        if (cur & 0xFF) + blocks > TRACK_BLOCKS:
            cur = (cur & ~0xFF) + 0x100      # no LB crosses a track: skip
            lb.sot = True
        cur += blocks
    # NUM_CONT counts the skipped blocks too
    ncont = cur - mm_start
    crossed = ncont > 0 and (mm_start & ~0xFF) != ((cur - 1) & ~0xFF)
    return ncont, crossed


# ---------------------------------------------------------------------------
# Table generation
# ---------------------------------------------------------------------------

@dataclass
class PhaseTables:
    gpt: list[int]               # GPT_SIZE halfwords
    cpha: list[int]              # CPHA_SIZE halfwords
    notes: list[str] = field(default_factory=list)


def generate(mmu_root: Path, con80: Path) -> PhaseTables:
    mmu_root, con80 = Path(mmu_root), Path(con80)
    if not con80.is_dir():
        raise StampError(f"CON80 deck not found: {con80} (use --con80)")
    areas = {a: parse_area(con80, a) for a in (1, 2, 3)}
    ipl = ipl_phases(con80)
    deck = ConcardDeck(con80)
    assigned = sorted(p for p in areas[1].mm_of_phase if 3 <= p <= 18)

    notes: list[str] = []
    per_phase: dict[int, tuple[list[LoadBlock], int, int, bool]] = {}
    for p in range(3, 19):
        if p not in assigned:
            lb = LoadBlock(0, 0, False)      # placeholder, emitted verbatim
            per_phase[p] = ([lb], 0, 1, False)
            notes.append(f"phase {p}: unassigned (MMB placeholder row)")
            continue
        lib_path = mmu_root / f"PHASE{p:02d}.lib"
        if not lib_path.exists():
            raise StampError(f"{lib_path} missing -- cannot stamp phase "
                             f"tables (phase {p} is MMUSYS-assigned)")
        lib = LibModule.read(lib_path)
        parent = POOL_PARENT.get(p, 2)
        parent_pool = pool_next_hw(
            LibModule.read(mmu_root / f"PHASE{parent:02d}.lib"))
        sym = json.load(open(mmu_root / f"PHASE{p:02d}"
                             / f"PHASE{p:02d}.sym.json"))
        look = protection_lookup(sym, deck_protection(deck, f"OFTMP@{p}"))
        lbs = derive_load_blocks(lib, parent_pool, look, himem=p in ipl)
        mm = areas[1].mm_of_phase[p]
        ncont, crossed = pack_mm(lbs, mm)
        per_phase[p] = (lbs, mm, ncont, crossed)
        notes.append(f"phase {p}: {len(lbs)} LBs, mm {mm:04X}, "
                     f"ncont {ncont}{' multi-track' if crossed else ''}")

    gpt = [0] * GPT_SIZE
    disp = 64
    for p in range(3, 19):
        lbs, mm, ncont, crossed = per_phase[p]
        base = 4 * (p - 3)
        gpt[base:base + 4] = [disp, len(lbs), mm,
                              ncont | (0x8000 if crossed else 0)]
        for lb in lbs:
            words = (PLACEHOLDER_LB if p not in assigned else lb.words())
            if disp + 3 > GPT_SIZE:
                raise StampError(f"#PFCMGPT overflow at phase {p} "
                                 f"(disp {disp})")
            gpt[disp:disp + 3] = list(words)
            disp += 3

    cpha = []
    for p in range(3, 22):
        for a in (1, 2, 3):
            if p in assigned:
                cpha.append(areas[a].mm_of_phase[p])
            elif p <= 18:
                cpha.append(0)               # unassigned (11, 17)
            else:
                cpha.append(FILL)            # MMB staging fill rows 19-21
    return PhaseTables(gpt, cpha, notes)


# ---------------------------------------------------------------------------
# Image stamping
# ---------------------------------------------------------------------------

def stamp(image: bytes, sym: dict, tables: PhaseTables) -> bytes:
    """Overlay the generated tables at the csects' composed addresses."""
    at = {s["name"]: s for s in sym.get("sections", [])}
    buf = bytearray(image)
    for name, words, want in ((GPT_CSECT, tables.gpt, GPT_SIZE),
                              (CPHA_CSECT, tables.cpha, CPHA_SIZE)):
        sec = at.get(name)
        if sec is None:
            raise StampError(f"{name} not present in the composed image -- "
                             f"is phase 2 in the composition?")
        if sec["size"] < want:
            raise StampError(f"{name} is {sec['size']} hw, expected >= {want}")
        off = sec["address"] * 2
        buf[off:off + 2 * want] = b"".join(
            w.to_bytes(2, "big") for w in words)
    return bytes(buf)
