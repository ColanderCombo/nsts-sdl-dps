#!/usr/bin/env python3
"""mmbstamp -- generate the Mass-Memory-Build-stamped phase tables.

The mass memory build process generates two CSECTS containing tables
with MM content information:

  #PFCMGPT (1,093 hw)  in-core phase table: 16 4-hw phase descriptors
                       (phases 3..18) + packed 3-hw load-block descriptors
  #PCDCPHA (57 hw)     alternate-MM-area phase table

Generation rules:

  * Load blocks = contiguous checksum-group runs of the phase's linkedit
    image.  A group is one or more .lib TEXT extents plus the LE's fill,
    followed by its 2-hw fullword-aligned checksum slot, which the LB
    INCLUDES: lb_len = roundup_fullword(extent_end) + pad - start + 2.
    Adjacent groups (next start == previous group end incl. slot) merge
    into one LB while total length stays <= 16384 hw (32 MMU blocks) and
    bank and block protection agree; a single group may exceed 16384 and
    then stands alone.  The cap is measured on where the merged LB would
    END -- content plus the new tail's own pad plus the checksum -- since
    the pad is part of the block the MMB writes.  A group headed by an
    undeclared `#T`/`$T` reserved patch area (`deck_standalone`) never
    merges backwards.
  * The fill (FillRule): a group whose tail csect the LE placed itself --
    HAL library data, an autocalled compool, a STACK-card stack, i.e. not
    a deck INSERT -- is padded by up to 2048 hw when the free space past
    it runs on to a deck-PINNED segment of this phase's own (an OVERLAY
    card with a location field) in the same bank, with no other phase's
    csect in between; a bank's last group is never padded this way.
    A pad that reaches the pinned segment joins the two into ONE group; a
    pad that stops at 2048 hw ends its group there.
  * The Z1 ZCON pool is one LB per phase: [own pool start, zconPoolNext)
    where zconPoolNext (the .lib LSTA cursor, which already includes the
    pool's closing checksum slot) comes from the phase and the pool start
    from its first pool extent at/above the parent phase's cursor;
    re-supplies of parent-phase pool cells below that are not tape
    payload and are dropped.  The region is bounded below by
    Z1_POOL_START_HW; under it is the PSA, which a phase supplies like any
    other content.
  * Upper-memory (sector >= 8) extents are listed only for IPL-set phases
    (MMLOAD IPL,PH=(...)): the overlay BCE builder ignores HIMEM LBs
    and only the SSL loads upper memory at IPL.
  * MC bank tails: a phase that anchors a memory configuration (MC=n on
    its MMUSYSn PHASE card) has each bank's final load block run through
    the bank end whenever the free run from its content to the boundary
    is at most one LBLN staging buffer (2048 hw); non-MC phases leave the
    tail free.
  * The bank-tail backup (_open_bank_tails) measures the free run from
    the last occupant of the MC below the block.
  * A phase may not exceed its MMUDATn `ALLOC BLKS`: the bank-tail fill is
    tape payload, so when the packed span overruns the allocation the tails
    are backed up less (highest bank first, in BANK_TAIL_STEP units) until
    NUM_CONT fits.
  * MM packing: starting at the phase's ALLOC address (MMUDATn decks,
    ADDR=FTSBB -> mm16 = F<<11|T<<8|S<<5|BB; +5 blocks for PHTAB=YES
    phases -- the on-MMU phase-table copy precedes the phase data), each
    LB takes ceil(len/512) 512-hw blocks, back to back; an LB may not
    cross a track (256-block) boundary: it skips to the next track and
    carries the SOT flag.  NUM_CONT_MM_BLKS counts data blocks plus
    track-skip waste; bit0 (0x8000) = the span crossed a track boundary.
  * RESERVE regions: a csect a `RESERVE` card allocates carries no text,
    so the load module has no extent for it, and it still takes a
    load-block descriptor -- allocation only, flags bit 2 (X'2000'), no
    checksum slot, no tape payload, no MM blocks, never merged into a
    group carrying any.  The loader unprotects a phase's load blocks
    before it reads, and the memory fill ahead of the load leaves
    everything above the IPL program protected; one such region holds the
    loader's transfer buffers.
  * Unassigned phases get the placeholder the MMB emits: 
    one LB descriptor (8000, 0610, 0004), mm addr 0, ncont 1.
"""
from __future__ import annotations

import bisect
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ap101Utils.concard import (ConcardDeck, expand_directives,
                                layout_program)
from ap101Utils.conlayout import (Z1_POOL_START_HW,
                                  default_protected as _class_default)
from ap101Utils.libModule import LibModule

GPT_SIZE = 1093                  # #PFCMGPT halfwords (64 + 3*343)
CPHA_SIZE = 57                   # #PCDCPHA halfwords (19 rows x 3 areas)
GPT_CSECT = "#PFCMGPT"
CPHA_CSECT = "#PCDCPHA"

# The IPL phase table the bootstrap loader hands the System Software
# Loader: one 0x100-hw area per PASS copy, three copies back to back.
# Each area is a 3-hw descriptor for every phase of the IPL set --
# halfword index of its first load-block descriptor, load-block count, MM
# address of its first load block -- followed by the load-block
# descriptors themselves, packed in the same phase order.  Descriptor
# halfword 0 doubles as the "this system was mass-memory built" flag: the
# loader skips the whole load when it is zero.
SSLPT_CSECT = "FCMSSLPT"
SSLPT_STRIDE = 0x100             # halfwords per PASS-copy area
SSLPT_COPIES = 3                 # PASS copies 1..3 = MM areas 1..3
SSLPT_SIZE = SSLPT_STRIDE * SSLPT_COPIES

# The copy of that table the bootstrap loader carries, three areas of the
# same stride inside csect FCMBOOT.  Halfword zero of an unstamped area
# reads X'FFFF', the signal that the area was never mass memory built.
BOOT_PT_SYMS = ("FCMPTAD1", "FCMPTAD2", "FCMPTAD3")

# Length and checksum of the loader itself, summed over [SSLSTART, SSLEND)
# as 16-bit halfwords.  The copy in the image is checked before control
# reaches the loader, so these are stamped last, over the finished image.
CKSUM_CSECT = "FCMCKSUM"
CKSUM_LENGTH_OFF = 0             # SSLENGTH
CKSUM_SUM_OFF = 3                # SSLCKSUM
SSL_START_SYM = "SSLSTART"
SSL_END_SYM = "SSLEND"

BANK_HW = 0x8000                 # halfwords per 32K bank (one DSR value)
LB_CAP_HW = 16384                # merge cap: 32 MMU blocks; single checksum
                                 # groups may exceed it
BLOCK_HW = 512                   # MMU block
RESERVE_BIT = 0x2000             # load-block flags: allocation only
TRACK_BLOCKS = 256               # blocks per track (track = +0x100 in mm16)
HIMEM_SECTOR = 8                 # FCMHIMEM: sector >= 8 = upper memory
FILL = 0xC6C6                    # MMB staging-buffer fill (INIT=C6C6)
FILL_PAD_HW = 2048               # pad past an auto-placed group tail; also
                                 # the MMB load-block length (MMUSYSn LBLN)
STACK_MODULE = "<generated-stacks>"   # lnk101's auto-generated @-stacks
BANK_TAIL_STEP = 1024            # step the MMB backs a bank's last block up by
PAD_REOPEN_MAX_HW = 64

# Placeholder LB descriptor the MMB emits for unassigned phases (11, 17):
# addr 8000, flags 0610, len 4.
PLACEHOLDER_LB = (0x8000, 0x0610, 4)

# Pool-cursor parent: the phase loaded before this one in its memory
# configuration. The Z1 ZCON pool is cursor-allocated along that chain, 
# so a phase's own pool cells start at its parent's zconPoolNext.
POOL_PARENT = {3: 2, 4: 3, 5: 3, 6: 3, 7: 3, 8: 3, 9: 2, 10: 2, 12: 9,
               13: 2, 14: 2, 15: 14, 16: 14, 18: 8}


class StampError(Exception):
    pass


# ---------------------------------------------------------------------------
# CON80 side: MMUSYSn / MMUDATn / MMLOAD
# ---------------------------------------------------------------------------

_PHASE_RE = re.compile(r"^(\S+)\s+PHASE,(\S+)")
_ALLOC_RE = re.compile(r"^(\S+)\s+ALLOC,ADDR=(\d{5})(?:,BLKS=(\d+))?")
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
    mc_phases: set = field(default_factory=set)        # phases with MC=n
    blks_of_phase: dict = field(default_factory=dict)  # phase -> ALLOC BLKS
                                                       # available for DATA
                                                       # (less the PHTAB copy)


def parse_area(con80: Path, area: int) -> AreaTable:
    out = AreaTable(area)
    members: dict[str, tuple[int, bool]] = {}          # member -> (ph, phtab)
    for ln in (con80 / f"MMUSYS{area}").read_text(errors="replace").splitlines():
        m = _PHASE_RE.match(ln)
        if not m:
            continue
        opts = m.group(2).rstrip(";")
        ph = phtab = mc = None
        for kv in opts.split(","):
            k, _, v = kv.partition("=")
            if k == "PH":
                ph = int(v)
            elif k == "PHTAB":
                phtab = v.startswith("Y")
            elif k == "MC":
                mc = int(v)
        if ph is not None:
            members[m.group(1)] = (ph, bool(phtab))
            if mc is not None:
                out.mc_phases.add(ph)
    allocs: dict[str, int] = {}
    blks: dict[str, int] = {}
    for ln in (con80 / f"MMUDAT{area}").read_text(errors="replace").splitlines():
        m = _ALLOC_RE.match(ln)
        if m:
            allocs[m.group(1)] = mm16(m.group(2))
            if m.group(3):
                blks[m.group(1)] = int(m.group(3))
    for member, (ph, phtab) in members.items():
        if member in allocs:
            out.mm_of_phase[ph] = allocs[member] + (5 if phtab else 0)
            if member in blks:
                # NUM_CONT is counted from the phase's DATA start, which is
                # already 5 blocks past the allocation for a PHTAB=YES
                # phase: that on-MMU phase-table copy is part of BLKS.
                out.blks_of_phase[ph] = blks[member] - (5 if phtab else 0)
    return out


def ipl_load_order(con80: Path) -> tuple[int, ...]:
    """The IPL-loaded phases in card & load order (IPL,PH=(...)).
    """
    for ln in (con80 / "MMLOAD").read_text(errors="replace").splitlines():
        m = _IPL_RE.search(ln)
        if m and not ln.lstrip().startswith("*"):
            return tuple(int(p) for p in m.group(1).split(","))
    raise StampError(f"{con80}/MMLOAD: no IPL,PH=(...) card")


def ipl_phases(con80: Path) -> set[int]:
    """The IPL-loaded phase set from the MMLOAD card (IPL,PH=(...))."""
    return set(ipl_load_order(con80))


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


def deck_reserved(deck: ConcardDeck, root: str) -> set[str]:
    """The csects a RESERVE card allocates without loading any text."""
    return {op.operand for op in layout_program(deck, root)
            if op.verb == "RESERVE"}


def reserve_regions(sym: dict, names: set[str]) -> list[tuple[int, int]]:
    """[start, end) of each RESERVE-carded csect the phase placed."""
    return sorted(
        (s["address"], s["address"] + s["size"])
        for s in sym.get("sections", ())
        if str(s.get("name", "")).strip() in names
        and s.get("address") is not None and s.get("size"))


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


def deck_inserted(deck: ConcardDeck, root: str) -> frozenset[str]:
    """Names the deck places itself."""
    return frozenset(op.operand for op in layout_program(deck, root)
                     if op.verb in ("INSERT", "INCLUDE"))


def map_phases(deck: ConcardDeck, root: str) -> frozenset[int]:
    """Phase numbers this root's deck segment MAPs."""
    out: set[int] = set()
    for _, d in expand_directives(deck, root):
        if d.is_directive and d.verb == "MAP" and d.operand:
            try:
                out.add(int(d.operand.split(",")[0]))
            except ValueError:
                pass
    return frozenset(out)


def map_extents(mmu_root: Path, deck: ConcardDeck, root: str
                ) -> dict[int, int]:
    """start hw -> the furthest end any MAP-carded parent phase reaches from
    that same start."""
    out: dict[int, int] = {}
    for ph in sorted(map_phases(deck, root)):
        p = Path(mmu_root) / f"PHASE{ph:02d}" / f"PHASE{ph:02d}.sym.json"
        if not p.exists():
            continue
        for s in json.load(open(p)).get("sections", []):
            if not s["size"]:
                continue
            end = s["address"] + s["size"]
            if end > out.get(s["address"], 0):
                out[s["address"]] = end
    return out


def region_origins(deck: ConcardDeck) -> dict[str, int]:
    """Every OVERLAY region the deck gives a location field, name -> hw
    address, over the entire deck rather than one root."""
    cached = getattr(deck, "_region_origins", None)
    if cached is not None:
        return cached
    out: dict[str, int] = {}
    for member in sorted(deck.members):
        for d in deck.read(member):
            if not d.is_directive or d.verb != "OVERLAY" or not d.operand \
                    or not d.label:
                continue
            try:                            # the location field, hex halfword
                out.setdefault(d.operand, int(d.label, 16))
            except ValueError:
                pass
    deck._region_origins = out
    return out


def deck_origins(deck: ConcardDeck, root: str) -> frozenset[int]:
    """Absolute addresses the deck pins a segment to """
    return frozenset(op.origin for op in layout_program(deck, root)
                     if op.origin is not None)


def deck_reopened(deck: ConcardDeck, root: str) -> frozenset[int]:
    """Addresses a bare `OVERLAY <name>` card INHERITS, because the region
    was declared with a location field elsewhere in the deck."""
    inherit = region_origins(deck)
    own = deck_origins(deck, root)
    return frozenset(
        inherit[op.operand] for op in layout_program(deck, root)
        if op.origin is None and op.verb == "OVERLAY"
        and op.operand in inherit and inherit[op.operand] not in own)


def deck_standalone(deck: ConcardDeck, root: str) -> frozenset[str]:
    """Reserved patch areas the deck leaves UNDECLARED, which the MMB gives
    a load block of their own (see `FillRule` / `derive_load_blocks`)."""
    out: list[str] = []
    pending: list[str] = []

    def finalize(marked=False):
        if not marked:
            out.extend(n for n in pending
                       if len(n) > 1 and n[0] in "#$" and n[1] == "T")
        pending.clear()

    for op in layout_program(deck, root):
        v = op.verb
        if v in ("OVERLAY", "BANK", "PHASE"):
            finalize()
        elif v in ("INSERT", "RESERVE", "STACK"):
            pending.append(op.operand)
        elif v in ("SET", "CLEAR"):
            finalize(True)
    finalize()
    return frozenset(out)


def fill_rule(sym: dict, deck: ConcardDeck, root: str,
              mapped: "dict[int, int] | None" = None) -> "FillRule":
    """The phase's FillRule from its sym.json and the CON80 deck"""
    return FillRule(
        sorted((s["address"], s["address"] + s["size"], s["name"])
               for s in sym.get("sections", []) if s["size"]),
        deck_inserted(deck, root),
        deck_origins(deck, root),
        deck_standalone(deck, root),
        deck_reopened(deck, root),
        mapped or {})


@dataclass
class FillRule:
    """Where a checksum group runs past its last csect.

    A group whose tail csect the LE placed itself (HAL library, autocall,
    STACK -- not a deck INSERT) is padded when the free space beyond it
    runs on to a deck-pnned segment of this phase's own (a patch overlay
    with a location field) in the same bank, with no other phase's csect
    in between; the pad is min(FILL_PAD_HW, that distance).  A pad that
    reaches the pinned segment makes the two ONE group (no checksum in
    between); one that stops at FILL_PAD_HW closes its group there.
    """
    sections: list                          # (start, end, name) sorted, ours + MAP
    deck_placed: frozenset                  # deck_inserted() names
    origins: frozenset                      # deck_origins() addresses
    standalone: frozenset = frozenset()     # deck_standalone() names
    reopened: frozenset = frozenset()       # deck_reopened() addresses
    mapped: dict = field(default_factory=dict)   # map_extents(): start -> end

    def __post_init__(self):
        self._starts = [s[0] for s in self.sections]
        self._standalone_at = frozenset(
            s for s, _, n in self.sections if n in self.standalone)

    def free_after(self, hw: int, limit: int) -> int:
        """Free halfwords at hw, up to limit (the bank end)."""
        i = bisect.bisect_right(self._starts, hw)
        for s, e, _ in reversed(self.sections[:i]):
            if s <= hw < e:               # scan them all: this is asked at
                return 0                  # addresses up to 2048 hw past a
                                          # group, past csects of any length
        nxt = self._starts[i] if i < len(self._starts) else limit
        return max(0, min(nxt, limit) - hw)

    def starts_block(self, hw: int) -> bool:
        """Does a csect that must head its own load block start at hw?
        (`deck_standalone`: an undeclared `#T`/`$T` reserved patch area.)"""
        return hw in self._standalone_at

    def tail_at(self, hw: int) -> str | None:
        """Name of the section ending at hw (a group's tail csect)."""
        i = bisect.bisect_right(self._starts, hw) - 1
        for s, e, n in reversed(self.sections[max(0, i - 40):i + 1]):
            if e == hw:
                return n
        return None

    def occupied_below(self, hw: int, floor: int) -> int:
        """Slot-rounded end of the last section ending in (floor, hw]:
        the memory configuration's last occupant below hw.  Returns 
        floor when nothing ends in the window."""
        best = floor
        i = bisect.bisect_left(self._starts, hw)
        for s, e, _ in self.sections[:i]:
            e = max(e, self.mapped.get(s, 0))
            if e <= hw:
                end = roundfw(e) + 2               # its checksum slot
                if best < end <= hw:
                    best = end
        return best

    def pad(self, tail: str | None, end: int, own_next: int,
            bank_end: int, start: int | None = None,
            reach: bool = False) -> int:
        if tail is None:
            return 0
        if tail in self.deck_placed and not reach:
            return 0
        if own_next >= bank_end:           # nothing left in THIS bank: the
            return 0                       # bank-tail rules own the run, and
                                           # the pinned segment the origins
                                           # test would find sits in the NEXT
                                           # bank 
        target = own_next
        if target not in self.origins:     # only toward a pinned segment
            if target not in self.reopened:
                return 0
            if start is None or end - start >= PAD_REOPEN_MAX_HW:
                return 0
        if target != end + self.free_after(end, bank_end):
            return 0                       # another phase's csect in between
        if reach and target - end > FILL_PAD_HW:
            return 0                       # the run must reach the segment,
                                           # not stop at the buffer size
        return min(FILL_PAD_HW, target - end)


def pool_next_hw(lib: LibModule) -> int:
    return lib.linkState.get("zconPoolNext", 0) // 2


@dataclass
class LoadBlock:
    start: int                   # effective hw address (sector*0x8000+off)
    length: int                  # hw, INCLUDING the 2-hw checksum tail
    protected: bool
    sot: bool = False            # start-of-track (set by MM packing)
    content_start: int | None = None   # set when _open_bank_tails backed the
                                       # block up over free memory: the lowest
                                       # halfword it must still cover
    reserve: bool = False        # allocation only: no data on the tape

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
        if self.reserve:
            flags |= RESERVE_BIT
        return addr, flags, self.length


def pool_own_start(mmu_root: Path, phase: int) -> int:
    """Where a phase's Z1 pool cells begin."""
    parent = POOL_PARENT.get(phase, 2)
    if parent == phase:
        return Z1_POOL_START_HW
    return pool_next_hw(LibModule.read(Path(mmu_root)
                                       / f"PHASE{parent:02d}.lib"))


def derive_load_blocks(lib: LibModule, parent_pool: int, look,
                       himem: bool, fill: FillRule | None = None,
                       mc: bool = False,
                       reserves: 'list[tuple[int, int]] | None' = None
                       ) -> list[LoadBlock]:
    """The phase's LB partition from its .lib extents (rules in the module
    docstring).  Without a FillRule the groups are csect-tight.  mc=True
    marks a memory-configuration anchor phase (MC=n on its PHASE card),
    whose bank tails are padded through the bank end.  `reserves` are the
    phase's allocation-only regions."""
    pool = pool_next_hw(lib)
    # [start, content_end, protected, tail csect, prot votes true/total,
    #  reserve]
    units: list[list] = []
    pool_start = None
    for x in sorted(lib.extents, key=lambda x: x.address):
        s, e = x.address // 2, x.address // 2 + x.hwLength
        if Z1_POOL_START_HW <= s and e <= pool:    # Z1 pool area extent
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
            units.append([s, roundfw(pe), False,
                          fill.tail_at(pe) if fill else None,
                          sum(votes), len(votes), False])
            s = pe
    if pool_start is not None:               # pool: ZCON constants; its
        units.append([pool_start, pool - 2, True, None, 1, 1, False])
    # A RESERVE-carded csect has no text and no extent naming it
    for rs, re_ in (reserves or ()):
        if not himem and rs >= HIMEM_SECTOR * BANK_HW:
            continue
        s = rs
        while s < re_:                       # split at bank boundaries
            bnd = (s // BANK_HW + 1) * BANK_HW
            pe = min(re_, bnd)
            votes = [v for v in (look(h) for h in range(s, pe))
                     if v is not None]
            units.append([s, pe, False, None, sum(votes), len(votes), True])
            s = pe
    units.sort()                             # the closing slot in it already

    # Coalesce the fullword pad the linker leaves between an odd-length
    # csect and the next one: the load module carries no text for it, but
    # it is one checksum group, so the 16384-hw cap must see the whole run
    # and not the fragments.
    runs: list[list] = []
    for u in units:
        if (runs and u[0] == runs[-1][1] and not u[6] and not runs[-1][6]
                and u[0] // BANK_HW == runs[-1][0] // BANK_HW):
            c = runs[-1]
            c[1], c[3] = u[1], u[3]
            c[4] += u[4]
            c[5] += u[5]
            continue
        runs.append(list(u))
    units = runs
    for u in units:                          # protection: majority by hw
        u[2] = u[4] * 2 > u[5] if u[5] else False

    starts = [u[0] for u in units]

    def pad(u, reach: bool = False) -> int:
        if fill is None:
            return 0
        bnd = (u[0] // BANK_HW + 1) * BANK_HW
        i = bisect.bisect_left(starts, u[1])
        return fill.pad(u[3], u[1], starts[i] if i < len(starts) else bnd,
                        bnd, u[0], reach)

    def close(u) -> int:
        """End of the LB: the 2-hw checksum slot follows the pad when the
        free space has room for it, else it overlays the pad's last 2 hw."""
        if u[6]:
            return u[1]
        p = pad(u)
        bnd = (u[0] // BANK_HW + 1) * BANK_HW
        room = fill.free_after(u[1], bnd) if fill else 2
        return min(u[1] + (p + 2 if p + 2 <= room else max(p, 2)), bnd)

    merged: list[list] = []
    for u in units:
        if merged:
            c = merged[-1]
            p = pad(c)
            # A fill run that REACHES a deck-pinned segment is one group with
            # it:
            q = p or pad(c, reach=True)
            # the pad ran into the next unit (one group), or the two groups
            # are back to back with the earlier one's checksum between them.
            # The cap is measured on what the merged LB would actually END
            # at:
            if (u[0] in (c[1] + p, c[1] + p + 2, c[1] + q, c[1] + q + 2)
                    and not u[6] and not c[6]
                    and u[2] == c[2]
                    and u[0] // BANK_HW == c[0] // BANK_HW
                    and not (fill and fill.starts_block(u[0]))
                    and close([c[0], u[1], u[2], u[3], 0, 0, False])
                        - c[0] <= LB_CAP_HW):
                c[1], c[3] = u[1], u[3]
                continue
        merged.append(list(u))

    lbs = [LoadBlock(u[0], close(u) - u[0], u[2], reserve=u[6])
           for u in merged]
    if fill:
        lbs = _open_bank_tails(lbs, fill)
        if mc:
            lbs = _extend_mc_bank_tails(lbs, fill)
    return lbs


def _extend_mc_bank_tails(lbs: list[LoadBlock], fill: FillRule
                          ) -> list[LoadBlock]:
    """An MC (memory-configuration anchor) phase pads each bank's final
    load block through the bank end when the free run past its content is
    at most one LBLN staging buffer (FILL_PAD_HW)"""
    out = []
    for i, lb in enumerate(lbs):
        if lb.reserve:
            out.append(lb)
            continue
        bnd = (lb.start // BANK_HW + 1) * BANK_HW
        end = lb.start + lb.length
        nxt = lbs[i + 1].start if i + 1 < len(lbs) else None
        gap = bnd - end
        if ((nxt is None or nxt >= bnd) and 0 < gap <= FILL_PAD_HW
                and bnd - lb.start <= LB_CAP_HW
                and fill.free_after(end, bnd) >= gap):
            out.append(LoadBlock(lb.start, bnd - lb.start, lb.protected,
                                 lb.sot))
        else:
            out.append(lb)
    return out


def _open_bank_tails(lbs: list[LoadBlock], fill: FillRule) -> list[LoadBlock]:
    """A block that ENDS a bank starts earlier than its content does: the
    MMB backs it up over free memory in 1024-hw steps until what is left of
    the bank fits one LBLN-sized (FILL_PAD_HW) load block.

    The step and the cap are both MMB quantities: `LBNO x LBLN` on an
    `MMUSYSn PHASE` card equals the card's `ALLOC BLKS x 512`, which puts
    LBLN in halfwords, and the one phase that declares it uses 2048."""
    out = []
    for i, lb in enumerate(lbs):
        if lb.reserve:
            out.append(lb)
            continue
        bnd = (lb.start // BANK_HW + 1) * BANK_HW
        prev_end = (lbs[i - 1].start + lbs[i - 1].length) if i else 0
        if (lb.start + lb.length != bnd            # does not end the bank
                or prev_end >= lb.start            # nothing to back up over
                or prev_end <= bnd - BANK_HW):     # predecessor not INSIDE
            out.append(lb)                         # this bank (ending at its
            continue                               # base line does not count)
        # Eligibility comes from the phase's OWN previous LB (above), but
        # the free run is bounded by the memory configuration's last
        # occupant: the MAP'd base csects in fill.sections count too.
        prev_end = fill.occupied_below(lb.start, prev_end)
        if prev_end >= lb.start:
            out.append(lb)
            continue
        avail = bnd - prev_end
        k = max(0, -(-(avail - FILL_PAD_HW) // BANK_TAIL_STEP))
        start = prev_end + k * BANK_TAIL_STEP
        if start >= lb.start or fill.free_after(prev_end, lb.start) < \
                lb.start - prev_end:
            out.append(lb)                 # someone else owns the run
            continue
        out.append(LoadBlock(start, bnd - start, lb.protected, lb.sot,
                             lb.start))
    return out


def fit_budget(lbs: list[LoadBlock], mm_start: int, budget: int | None
               ) -> tuple[int, bool]:
    """Pack, then hold the phase inside its MMUDATn `ALLOC BLKS`."""
    ncont, crossed = pack_mm(lbs, mm_start)
    if budget is None or ncont <= budget:
        return ncont, crossed
    for lb in reversed(lbs):
        if lb.content_start is None:
            continue                 # not a backed-up bank tail: no slack
        while (ncont > budget
               and lb.start + BANK_TAIL_STEP <= lb.content_start):
            lb.start += BANK_TAIL_STEP
            lb.length -= BANK_TAIL_STEP
            ncont, crossed = pack_mm(lbs, mm_start)
        if ncont <= budget:
            break
    return ncont, crossed


def pack_mm(lbs: list[LoadBlock], mm_start: int) -> tuple[int, bool]:
    """Assign MM blocks in table order from mm_start; sets each LB's SOT
    flag; returns (NUM_CONT_MM_BLKS, crossed_track)."""
    cur = mm_start
    for lb in lbs:
        lb.sot = False               # re-runnable: fit_budget packs repeatedly
        # An allocation-only block has nothing on the tape, so it consumes
        # no MM blocks and cannot start a track.
        blocks = 0 if lb.reserve else -(-lb.length // BLOCK_HW)
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
    g3dat: list[int] = field(default_factory=list)   # G3DAT_SIZE halfwords


@dataclass
class PhaseSource:
    areas: dict                  # area -> AreaTable
    ipl: set                     # IPL-loaded phases (MMLOAD)
    deck: ConcardDeck
    assigned: list               # MMUSYS-assigned phases, 3..18
    mc: set = field(default_factory=set)   # MC=n (config-anchor) phases


def load_phase_source(con80: Path) -> PhaseSource:
    con80 = Path(con80)
    if not con80.is_dir():
        raise StampError(f"CON80 deck not found: {con80} (use --con80)")
    areas = {a: parse_area(con80, a) for a in (1, 2, 3)}
    return PhaseSource(areas, ipl_phases(con80), ConcardDeck(con80),
                       sorted(p for p in areas[1].mm_of_phase if 3 <= p <= 18),
                       set().union(*(a.mc_phases for a in areas.values())))


def phase_load_blocks(mmu_root: Path, phase: int, src: PhaseSource
                      ) -> tuple[list[LoadBlock], int, int, bool]:
    """One phase's MM-packed load blocks: (LBs, mm16, NUM_CONT, crossed)."""
    mmu_root = Path(mmu_root)
    lib_path = mmu_root / f"PHASE{phase:02d}.lib"
    if not lib_path.exists():
        raise StampError(f"{lib_path} missing -- cannot derive load blocks "
                         f"(phase {phase} is MMUSYS-assigned)")
    lib = LibModule.read(lib_path)
    parent_pool = pool_own_start(mmu_root, phase)
    sym = json.load(open(mmu_root / f"PHASE{phase:02d}"
                         / f"PHASE{phase:02d}.sym.json"))
    root = f"OFTMP@{phase}"
    look = protection_lookup(sym, deck_protection(src.deck, root))
    fill = fill_rule(sym, src.deck, root, map_extents(mmu_root, src.deck, root))
    reserves = reserve_regions(sym, deck_reserved(src.deck, root))
    lbs = derive_load_blocks(lib, parent_pool, look,
                             himem=phase in src.ipl, fill=fill,
                             mc=phase in src.mc, reserves=reserves)
    mm = src.areas[1].mm_of_phase[phase]
    ncont, crossed = fit_budget(lbs, mm, src.areas[1].blks_of_phase.get(phase))
    return lbs, mm, ncont, crossed


def stack_holes(sym: dict) -> list[tuple[int, int]]:
    """[start, end) of every auto-generated @-stack csect in a phase."""
    return [(s["address"], s["address"] + s.get("size", 0))
            for s in sym.get("sections", [])
            if s.get("module") == STACK_MODULE
            and s.get("address") is not None and s.get("size")]


def tape_text(mmu_root: Path, phase: int) -> dict[int, int]:
    mmu_root = Path(mmu_root)
    text: dict[int, int] = {}
    for x in LibModule.read(mmu_root / f"PHASE{phase:02d}.lib").extents:
        base = x.address // 2
        for i in range(x.hwLength):
            text[base + i] = (x.data[2 * i] << 8) | x.data[2 * i + 1]
    sym_path = mmu_root / f"PHASE{phase:02d}" / f"PHASE{phase:02d}.sym.json"
    if sym_path.exists():
        for a, b in stack_holes(json.load(open(sym_path))):
            for h in range(a, b):
                text.pop(h, None)
    return text


def generate(mmu_root: Path, con80: Path) -> PhaseTables:
    mmu_root = Path(mmu_root)
    src = load_phase_source(con80)
    areas, assigned = src.areas, src.assigned

    notes: list[str] = []
    per_phase: dict[int, tuple[list[LoadBlock], int, int, bool]] = {}
    for p in range(3, 19):
        if p not in assigned:
            lb = LoadBlock(0, 0, False)      # placeholder, emitted verbatim
            per_phase[p] = ([lb], 0, 1, False)
            notes.append(f"phase {p}: unassigned (MMB placeholder row)")
            continue
        lbs, mm, ncont, crossed = phase_load_blocks(mmu_root, p, src)
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


    # Generate G3 archive table:
    from ap101Utils import g3dat as _g3dat
    try:
        dat = _g3dat.generate(mmu_root, con80)
        g3 = list(dat.words)
        notes.extend(dat.notes)
    except _g3dat.G3DatError as e:
        g3 = []
        notes.append(f"{_g3dat.G3DAT_CSECT}: not generated ({e})")
    return PhaseTables(gpt, cpha, notes, g3)


# ---------------------------------------------------------------------------
# Image stamping
# ---------------------------------------------------------------------------

def table_csects(tables: PhaseTables) -> list[tuple[str, list[int], int]]:
    """(csect, values, declared size) for every table that was generated."""
    from ap101Utils.g3dat import G3DAT_CSECT, G3DAT_SIZE
    todo = [(GPT_CSECT, tables.gpt, GPT_SIZE),
            (CPHA_CSECT, tables.cpha, CPHA_SIZE)]
    if tables.g3dat:
        todo.append((G3DAT_CSECT, tables.g3dat, G3DAT_SIZE))
    return todo


def stamp(image: bytes, sym: dict, tables: PhaseTables) -> bytes:
    """Overlay the generated tables at the csects' composed addresses."""
    at = {s["name"]: s for s in sym.get("sections", [])}
    buf = bytearray(image)
    for name, words, want in table_csects(tables):
        sec = at.get(name)
        if sec is None:
            raise StampError(f"{name} not present in the composed image -- "
                             f"is phase 2 in the composition?")
        if sec.get("residue"):
            # This CSECT has been overwritten by a CSECT in a later phase:
            continue
        if sec["size"] < want:
            raise StampError(f"{name} is {sec['size']} hw, expected >= {want}")
        off = sec["address"] * 2
        buf[off:off + 2 * want] = b"".join(
            w.to_bytes(2, "big") for w in words)
    return bytes(buf)


# ---------------------------------------------------------------------------
# The IPL set's tables: the phase table the SSL walks, and its own checksum
# ---------------------------------------------------------------------------

def generate_sslpt(mmu_root: Path, con80: Path) -> tuple[list[int], list[str]]:
    """The IPL phase table, SSLPT_SIZE halfwords: one area per PASS copy.

    The load blocks are the same partition the in-core phase table and the
    tape image are built from, so only the MM address differs between
    copies, each reading it from its area's ALLOC cards.
    """
    mmu_root, con80 = Path(mmu_root), Path(con80)
    src = load_phase_source(con80)
    order = ipl_load_order(con80)
    notes: list[str] = []

    lbs_of = {p: phase_load_blocks(mmu_root, p, src)[0] for p in order}
    out: list[int] = []
    for copy in range(1, SSLPT_COPIES + 1):
        mm_of = src.areas[copy].mm_of_phase
        missing = [p for p in order if p not in mm_of]
        if missing:
            raise StampError(f"area {copy} has no ALLOC for IPL phase(s) "
                             f"{missing} -- cannot address the load")
        descs: list[int] = []
        blocks: list[int] = []
        index = 3 * len(order)            # past the descriptors themselves
        for p in order:
            lbs = lbs_of[p]
            descs += [index, len(lbs), mm_of[p]]
            for lb in lbs:
                blocks += list(lb.words())
            index += 3 * len(lbs)
        area = descs + blocks
        if len(area) > SSLPT_STRIDE:
            raise StampError(f"IPL phase table for copy {copy} is "
                             f"{len(area)} hw, over the {SSLPT_STRIDE} hw "
                             f"area (phases {order}, "
                             f"{sum(len(lbs_of[p]) for p in order)} LBs)")
        notes.append(f"copy {copy}: {len(area)} of {SSLPT_STRIDE} hw, "
                     + ", ".join(f"ph {p} {len(lbs_of[p])} LB @"
                                 f"{mm_of[p]:04X}" for p in order))
        out += area + [0] * (SSLPT_STRIDE - len(area))
    return out, notes


def ssl_extent(sym: dict) -> tuple[int, int]:
    """[first halfword, count) of the loader the IPL checksums."""
    at = {s["name"]: s["address"] for s in sym.get("symbols", ())
          if s.get("name") is not None}
    try:
        start, end = at[SSL_START_SYM], at[SSL_END_SYM]
    except KeyError as e:
        raise StampError(f"{e.args[0]} not in the composed image -- is the "
                         f"IPL phase in the composition?") from None
    if end <= start:
        raise StampError(f"{SSL_END_SYM} {end:#x} is not above "
                         f"{SSL_START_SYM} {start:#x}")
    return start, end - start


def stamp_ipl(image: bytes, sym: dict, sslpt: list[int]) -> bytes:
    """Overlay the IPL phase table, then the loader's length and checksum."""
    at = {s["name"]: s for s in sym.get("sections", ())}
    buf = bytearray(image)

    sec = at.get(SSLPT_CSECT)
    if sec is None:
        raise StampError(f"{SSLPT_CSECT} not present in the composed image "
                         f"-- is the IPL phase in the composition?")
    if sec["size"] < SSLPT_SIZE:
        raise StampError(f"{SSLPT_CSECT} is {sec['size']} hw, expected "
                         f">= {SSLPT_SIZE}")
    off = sec["address"] * 2
    buf[off:off + 2 * len(sslpt)] = b"".join(
        w.to_bytes(2, "big") for w in sslpt)

    start, count = ssl_extent(sym)
    total = sum(int.from_bytes(buf[2 * h:2 * h + 2], "big")
                for h in range(start, start + count)) & 0xFFFF
    sec = at.get(CKSUM_CSECT)
    if sec is None:
        raise StampError(f"{CKSUM_CSECT} not present in the composed image")
    if sec["size"] <= CKSUM_SUM_OFF:
        raise StampError(f"{CKSUM_CSECT} is {sec['size']} hw, expected "
                         f"> {CKSUM_SUM_OFF}")
    for slot, value in ((CKSUM_LENGTH_OFF, count), (CKSUM_SUM_OFF, total)):
        h = sec["address"] + slot
        buf[2 * h:2 * h + 2] = value.to_bytes(2, "big")
    return bytes(buf)


def ssl_checksum_slots(text: dict[int, int],
                       sym: dict) -> list[tuple[int, int]]:
    at = {s["name"]: s for s in sym.get("sections", ())}
    sec = at.get(CKSUM_CSECT)
    if sec is None or sec["size"] <= CKSUM_SUM_OFF:
        return []
    try:
        start, count = ssl_extent(sym)
    except StampError:
        return []
    if any(h not in text for h in (start, start + count - 1)):
        return []                     # the loader is not this phase's text
    total = sum(text.get(h, 0) for h in range(start, start + count)) & 0xFFFF
    return [(sec["address"] + CKSUM_LENGTH_OFF, count),
            (sec["address"] + CKSUM_SUM_OFF, total)]


def stamp_boot(image: bytes, sym: dict, sslpt: list[int]) -> bytes:
    """Overlay the phase table onto the bootstrap loader's three areas.

    FCMPTAD1/2/3 are a 256-halfword area per PASS copy, reserved as a
    single 768 word space.
    """
    at = {s["name"]: s["address"] for s in sym.get("symbols", ())
          if s.get("name") is not None}
    missing = [n for n in BOOT_PT_SYMS if n not in at]
    if missing:
        raise StampError(f"{', '.join(missing)} not in the composed image "
                         f"-- is the bootstrap phase in the composition?")
    addrs = [at[n] for n in BOOT_PT_SYMS]
    want = [addrs[0] + i * SSLPT_STRIDE for i in range(SSLPT_COPIES)]
    if addrs != want:
        raise StampError(
            f"{BOOT_PT_SYMS} are at "
            f"{', '.join(f'{a:#07x}' for a in addrs)}; the table is one "
            f"block of {SSLPT_COPIES} contiguous {SSLPT_STRIDE}-hw areas")
    buf = bytearray(image)
    off = addrs[0] * 2
    buf[off:off + 2 * len(sslpt)] = b"".join(
        w.to_bytes(2, "big") for w in sslpt)
    return bytes(buf)


# ---------------------------------------------------------------------------
# Load-module stamping
# ---------------------------------------------------------------------------

SIDECAR_SUFFIX = ".mmbstamp.json"   # beside PHASEnn.lib


@dataclass
class LibStamp:
    """One table written into (or restored to) one phase's load module."""
    phase: int
    csect: str
    address: int                 # halfword address
    before: list[int]            # what the load module carried
    after: list[int]             # what it carries now

    @property
    def changed(self) -> bool:
        return self.before != self.after


def sidecar_path(lib_path: Path) -> Path:
    """Where the pre-stamp halfwords for one phase are recorded."""
    return Path(lib_path).with_suffix(SIDECAR_SUFFIX)


def _phase_libs(mmu_root: Path) -> dict[int, Path]:
    out = {}
    for p in sorted(Path(mmu_root).glob("PHASE*.lib")):
        m = re.fullmatch(r"PHASE(\d+)", p.stem)
        if m:
            out[int(m.group(1))] = p
    return out


def _lib_sym(lib_path: Path) -> dict:
    """A phase's manifest, which is where the csect addresses come from."""
    stem = Path(lib_path).stem
    cand = Path(lib_path).parent / stem / f"{stem}.sym.json"
    if not cand.exists():
        cand = Path(lib_path).with_suffix(".sym.json")
    if not cand.exists():
        raise StampError(f"{lib_path}: no .sym.json beside it")
    return json.loads(cand.read_text())


def _extent_for(lib: LibModule, address: int, count: int):
    """The one text extent carrying [address, address+count), or None.

    A csect lies inside one extent, extents being contiguous runs of
    placed sections; straddling two means the manifest and the module
    disagree about where it is, and returns None.
    """
    for x in lib.extents:
        base = x.address // 2
        if base <= address and address + count <= base + x.hwLength:
            return x
    return None


def _splice(lib: LibModule, address: int, words: list[int],
            csect: str) -> list[int]:
    """Write `words` at halfword `address`; return what was there."""
    x = _extent_for(lib, address, len(words))
    if x is None:
        raise StampError(
            f"{csect} at {address:#x} ({len(words)} hw) is not inside one "
            f"text extent of {lib.name or 'the module'}")
    off = 2 * (address - x.address // 2)
    buf = bytearray(x.data)
    before = [int.from_bytes(buf[off + 2 * i:off + 2 * i + 2], "big")
              for i in range(len(words))]
    buf[off:off + 2 * len(words)] = b"".join(
        w.to_bytes(2, "big") for w in words)
    x.data = bytes(buf)
    return before


def stamp_libs(mmu_root: Path, tables: PhaseTables, *,
               dry_run: bool = False) -> list[LibStamp]:
    """Write the generated tables into the phase load modules that carry
    them, recording what each one held so `restore_libs` can undo it.
    """
    mmu_root = Path(mmu_root)
    libs = _phase_libs(mmu_root)
    if not libs:
        raise StampError(f"no PHASE*.lib in {mmu_root}")

    wanted = table_csects(tables)
    modules: dict[int, LibModule] = {}
    owners: dict[str, list[int]] = {name: [] for name, _, _ in wanted}
    secs: dict[tuple[int, str], dict] = {}
    for n, path in sorted(libs.items()):
        at = {s["name"]: s for s in _lib_sym(path).get("sections", [])}
        if not any(name in at for name, _, _ in wanted):
            continue
        lib = modules[n] = LibModule.read(path)
        for name, _, want in wanted:
            sec = at.get(name)
            if sec is None:
                continue
            if _extent_for(lib, sec["address"], want) is None:
                continue                      # declared, not carried
            owners[name].append(n)
            secs[(n, name)] = sec

    for name, _, _ in wanted:
        if not owners[name]:
            raise StampError(
                f"no phase load module in {mmu_root} carries {name} -- "
                f"the tables have nowhere to go")
        if len(owners[name]) > 1:
            raise StampError(
                f"{name} is carried by phases "
                f"{', '.join(map(str, owners[name]))} -- one load module "
                f"has to own it")

    out: list[LibStamp] = []
    for n in sorted(modules):
        mine = [(name, words, want) for name, words, want in wanted
                if (n, name) in secs]
        if not mine:
            continue
        path, lib = libs[n], modules[n]
        stamps: list[LibStamp] = []
        for name, words, want in mine:
            sec = secs[(n, name)]
            if sec["size"] < want:
                raise StampError(f"{name} is {sec['size']} hw in phase {n}, "
                                 f"expected >= {want}")
            before = _splice(lib, sec["address"], list(words), name)
            stamps.append(LibStamp(n, name, sec["address"], before,
                                   list(words)))
        if not dry_run:
            side = sidecar_path(path)
            if not side.exists():
                side.write_text(json.dumps(
                    {"module": path.name,
                     "tables": [{"csect": s.csect, "address": s.address,
                                 "before": s.before} for s in stamps]},
                    indent=1) + "\n")
            lib.write(path)
        out.extend(stamps)
    return out


def restore_libs(mmu_root: Path, *, dry_run: bool = False) -> list[LibStamp]:
    mmu_root = Path(mmu_root)
    out: list[LibStamp] = []
    for n, path in sorted(_phase_libs(mmu_root).items()):
        side = sidecar_path(path)
        if not side.exists():
            continue
        rec = json.loads(side.read_text())
        lib = LibModule.read(path)
        for t in rec.get("tables", []):
            after = _splice(lib, t["address"], list(t["before"]), t["csect"])
            out.append(LibStamp(n, t["csect"], t["address"], after,
                                list(t["before"])))
        if not dry_run:
            lib.write(path)
            side.unlink()
    return out
