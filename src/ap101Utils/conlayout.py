#!/usr/bin/env python3
#
# Compute csect memory placement from a CON80 layout program.
#
# This is a ridiculously complicated stack of heuristics that were developed
# iteratively to "just get it working".  It's likely there's something MUCH
# simpler going on here that I just haven't seen yet.  This module will
# undoubtably change a lot.
#
# 
#   * addr OVERLAY name -> begin a region named <name> 
#                          at absolute halfword <addr>
#         e.g.: 00000 OVERLAY PSA -> 0
#               001A8 OVERLAY Z1 -> 424
#                     OVERLAY NOADDR -> keeps LC, placed after last section
# 
#   *      BANK n -> IF it changes the current bank, 
#                    default the counter to n * 0x8000 
#   * addr INSERT csect` -> place csect AND associated data csects of
#                           its owning module 
#                               - In module order
#                               - Fullword aligned, advancing the counter.  
#                               
#                           No csect is placed twice; subsequent INSERTS
#                           after the first are ignored.
#                           <addr> field will force the csect to that location.
#   * SET / CLEAR -> declare the store-protection of the PRECEDING block of
#                    csects (everything placed since the last SET/CLEAR):
#                    SET = protected, CLEAR = unprotected.  Csects in an
#                    unmarked trailing block default by class: CODE/ZCON
#                    protected, DATA and @-stacks unprotected.
#   * STACK csect -> stack allocation for a module's auto storage; s
#   * PHASE list -> begins a phase: a separately MMU-stored segment overlaid
#                   into memory at runtime.  
#
# The csect type code carries the underlying placement constraint:
#   CODE ($0 etc.) in sector >= 2 
#   DATA (#D #0 #P #E #L #X) in sectors 0-1, 
#   ZCON (#Z #Q) in the first 2K of sector 0.
#
from __future__ import annotations

from dataclasses import dataclass, field

BANK_SIZE_HW = 0x8000 # == 32K HW's, 64KB

# The `OVERLAY Z1` "magic" overlay is where the linkage editor GATHERS every
# ZCON csect (#Z<module>/#Q<lib> thunks) into one pool.  The LE reserves a
# FIXED region for that pool regardless of how many ZCONs the config actually
# uses; the boundary is hardcoded so the following overlay and everything
# after it land past the reserve instead of packing right behind the pool
# content.
Z1_MAGIC_OVERLAY = "Z1"
Z1_POOL_END_HW   = 0x51E   # DSES ($ZDSECLR) start == top of the Z1 pool region

# AUTOCALL / orphan-flush placement pins come from a linkorder.json pins
# file (LayoutEngine's link_order); without one the waves place in
# discovery order and no pinned streams apply.
from .linkorder import LinkOrder, McPins


def align_word(hw: int) -> int:
    return (hw + 1) & ~1


# Class-based protection default for csects in a block with no SET/CLEAR
# mark: code and link-time ZCON constants live in protected core; data and
# @-stack frames are runtime-written and stay unprotected.  Prefix rules
# mirror lnk101's zone classification (_ZONE_BY_PREFIX), plus '#R' (REMOTE
# data), which that table does not name.
_UNPROT_PREFIXES = ("@", "#D", "#P", "#0", "#E", "#L", "#R", "#X")


def default_protected(name: str) -> bool:
    return not name.strip().startswith(_UNPROT_PREFIXES)


@dataclass
class Placement:
    addresses: dict[str, int] = field(default_factory=dict)   # csect -> start (hw)
    bank_of: dict[str, int] = field(default_factory=dict)
    overlay_of: dict[str, str] = field(default_factory=dict)
    phase_of: dict[str, str] = field(default_factory=dict)     # csect -> phase list
    protected: dict[str, bool] = field(default_factory=dict)   # csect -> write-protected
    unknown_inserts: list[str] = field(default_factory=list)   # INSERTs w/o a module
    reserved: set = field(default_factory=set)     # RESERVE: alloc-only, no text
    log: list[str] = field(default_factory=list)


class LayoutEngine:
    """Replays a layout_program into a Placement.

    `occupied` is a list of [loHw, hiHw) intervals already holding content
    from earlier phases (the deck's MAP cards): sequentially placed csects
    flow around them.  `phase_regions` maps earlier-phase OVERLAY region
    names to their start halfword: an OVERLAY card with no origin naming a
    known region RE-OPENS it there (the deck idiom for replacing an earlier
    phase's content; regions kept intact are MAP'd instead and appear in
    `occupied`)."""

    def __init__(self, module_csects: dict[str, list[tuple[str, int]]],
                 *, align: bool = True,
                 occupied: 'list[tuple[int, int]] | None' = None,
                 phase_regions: 'dict[str, int] | None' = None,
                 existing: 'dict[str, int] | None' = None,
                 base_occupied: 'list[tuple[int, int]] | None' = None,
                 local_defs: 'set[str] | None' = None,
                 link_order: 'LinkOrder | None' = None):
        self.module_csects = module_csects
        self.align = align
        self.link_order = link_order or LinkOrder()
        self.occupied = sorted(occupied or [])
        self.phase_regions = phase_regions or {}
        # Csects already defined outside this link (earlier-phase MAP imports),
        # name -> halfword address: a library INCLUDE of one of these REPLACES
        # it in place instead of placing sequentially.
        self.existing = existing or {}
        # Csect names this job supplies -- every SD of a non-external module
        # of the link.:
        self.local_defs = local_defs
        # The co-resident base's full memory footprint ([lo, hi) hw extents of
        # every SD in the MAP-carded phases' libs).  Roll-in pool placement
        # packs into its free gaps; deliberately not part of `occupied`, so
        # re-opened-region REPLACEMENT content can still land inside it.
        self.base_occupied = sorted(base_occupied or [])
        # The image carries a 2-hw terminal checksum csect (#T0<pp>...)
        # right after the base's last content in each bank.  Our libs don't
        # emit #T csects, so reserve the slot here or roll-in packing would
        # start one fullword early.
        if self.base_occupied:
            bank_max: dict[int, int] = {}
            for lo, hi in self.base_occupied:
                b = lo // BANK_SIZE_HW
                bank_max[b] = max(bank_max.get(b, 0), hi)
            for b, hi in bank_max.items():
                ck = align_word(hi)
                self.base_occupied.append((ck, ck + 2))
            # The whole Z1 region up to Z1_POOL_END_HW is reserved for the
            # ZCON pool's per-config growth.  run() promotes this reserve
            # into `occupied` for decks that do not own the Z1 overlay
            # themselves (see the promotion note there).
            self.base_occupied.append((0x1A8, Z1_POOL_END_HW))
            self.base_occupied.sort()

    def _next_free(self, lc: int, size: int, ignore=None) -> int:
        """First address >= lc where [lc, lc+size) clears every occupied
        interval.  `ignore` (one exact [lo, hi) tuple) exempts that interval:
        the pinned-island path passes the promoted Z1 reserve, because a
        deck's explicit origin card overrides the pool-growth reserve -- the
        reserve only fences UNPINNED flow out of the pool region."""
        moved = True
        while moved:
            moved = False
            for lo, hi in self.occupied:
                if (lo, hi) == ignore:
                    continue
                if lc < hi and lc + size > lo:
                    lc = align_word(hi) if self.align else hi
                    moved = True
        return lc

    def run(self, program, autocall_units=None) -> Placement:
        out = Placement()
        lc = 0
        cur_bank: int = 0
        cur_overlay: str | None = None
        cur_phase: str | None = None
        # SET/CLEAR block-terminator semantics: csects accumulate here until
        # a mark card declares the whole block's protection; leftovers at end
        # fall back to the class default (see default_protected).
        unmarked: list[str] = []
        # Per-bank running location counters: a `BANK n` card RESUMES bank
        # n's fill point (defaulting to the bank start on first visit) --
        # it does NOT rewind to the bank start.  The deck interleaves banks.
        banklc: dict[int, int] = {}
        # A bank's resumable fill point tracks only NATURAL (unpinned) placement.
        # A pinned-origin overlay/insert jumps `lc` to an absolute address; the
        # content it places there is an ISLAND and must NOT ratchet the bank's
        # fill point up to its end, or a later unpinned overlay resuming the
        # bank would start after the pinned patch overlays instead of where
        # natural flow left off.  `in_pinned` gates the fill-point save, and
        # pinned content is registered as occupied so natural fill flows
        # AROUND (not over) the islands.
        in_pinned = False

        def switch_lc(new_lc: int) -> int:
            # remember the fill point of the bank we are leaving -- but only when
            # leaving NATURAL flow (a pinned->pinned or pinned->bank hop must not
            # overwrite the bank's saved natural fill point)
            if not in_pinned:
                banklc[lc // BANK_SIZE_HW] = lc
            return new_lc

        # Every csect the deck INSERTs by name (used to detect module siblings
        # that have NO own INSERT and must ride along with their module's card).
        all_inserts = {o.operand for o in program
                       if o.verb == "INSERT" and o.operand}

        # Z1 RESERVE PROMOTION.  For a link whose deck does NOT own the Z1
        # magic overlay (every roll-in deck -- only the base decks' LOWCORE
        # member carries `OVERLAY Z1`), the pool reserve moves from
        # `base_occupied` into `occupied` so it binds EVERY placement path:
        # the occupied-only post-BANK flow AND the ordinary
        # buffer_block/_next_free flush.  A Z1-OWNING deck must NOT get the
        # reserve in `occupied`: its pinned `OVERLAY Z1` island and the
        # following islands legitimately live there.
        z1_reserve = (0x1A8, Z1_POOL_END_HW)
        z1_promoted = None      # pinned islands _next_free past it (pin wins)
        if z1_reserve in self.base_occupied \
                and not any(o.verb == "OVERLAY"
                            and o.operand == Z1_MAGIC_OVERLAY
                            for o in program):
            self.occupied.append(z1_reserve)
            self.occupied.sort()
            z1_promoted = z1_reserve

        # ROLL-IN OVERLAY CENSUS.  A roll-in phase's NEW overlays (origin-less,
        # not-a-re-open OVERLAY cards) have two placement paths, and the
        # discriminator is the post-BANK-card GROUP they arrive in:
        #   * A bank whose post-BANK new-overlay group contains a #D/#X/#Y
        #     overlay: EVERY new overlay of that bank's group -- data and #PC
        #     alike -- FLOWS as a whole block, deck INSERT order, first-fit
        #     over `self.occupied` ONLY (see the flush_block flow branch).
        #   * Everything else -- pre-BANK new overlays and non-data banks --
        #     keeps the pre-existing paths below.
        # The census mirrors the OVERLAY-handler state machine below.
        rollin_ovl_count: dict[int, int] = {}
        flow_ovls: set[str] = set()
        if self.base_occupied:
            c_bank, c_prev, c_cur, c_reopen, c_new = 0, None, None, False, False
            # `c_sawbank`: a BANK card has been seen since the last NON-new
            # overlay (pin/re-open/Z1 exit) -- i.e. we are inside a post-BANK
            # new-overlay group.  A re-open closes the group.
            c_sawbank = False
            c_postbank = False          # current overlay admitted post-BANK
            c_ovl_bank = 0              # ...and the bank it was admitted in
            bank_new_ovls: dict[int, list[str]] = {}
            data_banks: set[int] = set()
            for o in program:
                if o.verb == "BANK":
                    try:
                        c_bank = int(o.operand)
                        c_sawbank = True
                    except (TypeError, ValueError):
                        # the main loop logs this card too; a silently
                        # mis-banked census could flip the pool/flow choice
                        out.log.append(f"census: unparsable BANK operand "
                                       f"{o.operand!r}; counts may be "
                                       f"mis-banked")
                elif o.verb == "OVERLAY":
                    c_prev, c_cur = c_cur, (o.operand or c_cur)
                    c_new = False
                    c_postbank = False
                    if o.origin is not None:
                        c_sawbank = False
                        continue
                    if c_prev == Z1_MAGIC_OVERLAY \
                            and o.operand != Z1_MAGIC_OVERLAY:
                        c_sawbank = False
                        continue
                    if o.operand in self.phase_regions:
                        c_reopen = True
                        c_sawbank = False
                    elif c_reopen:
                        c_new = True
                        rollin_ovl_count[c_bank] = \
                            rollin_ovl_count.get(c_bank, 0) + 1
                        if c_sawbank:
                            c_postbank = True
                            c_ovl_bank = c_bank
                            bank_new_ovls.setdefault(c_bank, []).append(c_cur)
                elif o.verb == "INSERT" and c_new and c_postbank \
                        and o.origin is None and o.operand \
                        and o.operand.startswith(("#D", "#X", "#Y")) \
                        and o.operand not in self.existing:
                    data_banks.add(c_ovl_bank)
            for b in data_banks:
                flow_ovls.update(bank_new_ovls.get(b, []))

        # DEFERRED @0 STACK placement.  The LE puts each generated @0<M>
        # stack at the END of the OVERLAY block that INSERTs its module's PDE
        # (#E<M>) -- NOT at the deck position of the STACK card.  So we
        # pre-scan the STACK cards for @0<M> + size, and when an overlay
        # places #E<M> (in register) we queue @0<M>; `close_overlay` lays the
        # queue down at the block tail (in #E-INSERT order), same bank, just
        # before the checksum.  A stack whose #E<M> is NOT INSERTed falls
        # back to the old STACK-handler fill-point placement.
        stack_decls: dict[str, int] = {}
        for o in program:
            if o.verb == "STACK" and o.operand:
                snm = "@" + o.operand[1:] if o.operand[:1] in "$@" else o.operand
                ssizes = self.module_csects.get(snm)
                ssz = dict(ssizes).get(snm) if ssizes else None
                if ssz is not None:
                    stack_decls[snm] = ssz
        stack_queue: list[str] = []

        # WHOLE-OVERLAY placement.  Each unpinned overlay places as a
        # CONTIGUOUS BLOCK at the lowest free region of its bank that the
        # whole block fits (occupancy-aware first-fit from the bank's
        # application floor) -- NOT csect-by-csect.  So a big overlay that
        # can't fit below the pinned patch islands skips PAST them as a unit,
        # while a small later overlay backfills the gap they left.  `pending`
        # buffers the current unpinned overlay's block; `app_floor[bank]` is
        # the first application fill point in that bank (below which lies the
        # reserved low-core), captured on the first block flush.
        pending: list[tuple[str, int]] = []
        app_floor: dict[int, int] = {}
        # After the Z1 magic pool + DSES (the reserved low-core), the next pinned
        # card marks where bank-0 APPLICATION content may begin; capture lc there
        # so unpinned blocks first-fit above the reserved region, not into it.
        app_floor_pending = False

        # ROLL-IN POOL (no current deck feeds it; kept as the pooling-bank
        # placement rule and defer trigger): a smallest-first per-bank pack.
        # Free gaps smaller than 4 hw are skipped: those exact-2-hw slots
        # hold the per-overlay checksums, which the base's lib does not
        # record.
        rollin_pool: list[tuple[int, int, str, int, str]] = []   # (bank, seq, name, size, overlay)
        # Non-pool csects of roll-in NEW overlays in a POOLING bank keep their
        # whole-block grouping but flush AFTER the pool, first-fit over the
        # base-aware gaps, so they cannot take the pool's fill point.
        rollin_blocks: list[tuple[int, str, list]] = []   # (bank, overlay, [(name, size)])
        rollin_ends: set[int] = set()   # LE-placed run tails: the load block
                                        # FillRule-pads from here to the next
                                        # pinned segment
        cur_rollin = False
        # The current overlay belongs to a post-BANK new-overlay group whose
        # bank holds a #D/#X/#Y overlay: its buffered block flows whole,
        # deck order, occupied-only first-fit (see the census above).
        cur_rollin_flow = False
        # A phase qualifies as roll-in only once it has RE-OPENED an earlier
        # phase's region: the roll-in decks place every previous-phase
        # overlay first, then their NEW overlays.  A base phase (MAPs earlier
        # content but re-opens nothing) never triggers this, so its own new
        # overlays keep whole-block placement.
        seen_reopen = False

        def _bank_gaps(bank, include_base=True):
            """Free [lo, hi) gaps of `bank`.  With `include_base` (the pool /
            deferred-block paths) gaps are over occupied + the base's flat
            footprint; without it (the post-BANK flow) gaps are over
            `self.occupied` ONLY -- MAP'd intervals, own placements, the Z1
            reserve and checksum slots.  Occupancy is CONFIG-DEPENDENT,
            which a flat base footprint cannot express."""
            ivs = self.occupied + self.base_occupied if include_base \
                else self.occupied
            merged: list[tuple[int, int]] = []
            for lo, hi in sorted(ivs):
                if merged and lo <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
                else:
                    merged.append((lo, hi))
            b_lo, b_hi = bank * BANK_SIZE_HW, (bank + 1) * BANK_SIZE_HW
            gaps = []
            cursor = b_lo
            for lo, hi in merged:
                if hi <= b_lo or lo >= b_hi:
                    continue
                if lo > cursor:
                    gaps.append((cursor, min(lo, b_hi)))
                cursor = max(cursor, hi)
            if cursor < b_hi:
                gaps.append((cursor, b_hi))
            return gaps

        def backfill_leftovers(names, sizeof, banks=(0, 1)):
            """Autocall csects no pin/stream/wave placed: the LE backfills
            them first-fit into the lowest free data-zone gap (banks 0-1)
            over `self.occupied` alone"""
            # A gap that opens at an LE-placed (roll-in) run's tail is not
            # free: the run's load block FillRule-pads from there to the
            # next segment:
            for n in sorted(names,
                            key=lambda n: (-(sizeof.get(n) or 0),
                                           n.encode("cp037", "replace"))):
                if n in out.addresses or n in all_inserts:
                    continue
                if self.local_defs is not None \
                        and n not in self.local_defs \
                        and n in self.existing:
                    continue
                sz = sizeof.get(n)
                if sz is None:
                    continue               # module not in this link
                a = None
                for bank in banks:
                    gaps = [g for g in _bank_gaps(bank, include_base=False)
                            if g[1] - g[0] >= 4        # not a checksum slot
                            and g[0] not in rollin_ends]   # not a FillRule pad
                    for g_lo, g_hi in gaps:
                        start = align_word(g_lo) if self.align else g_lo
                        if start + sz <= g_hi:
                            a = start
                            break
                    if a is not None:
                        break
                if a is None:
                    out.log.append(f"autocall-backfill {n}: no data-zone "
                                   f"gap; left to linker auto-placement")
                    continue
                out.addresses[n] = a
                out.bank_of[n] = a // BANK_SIZE_HW
                out.overlay_of[n] = cur_overlay
                out.phase_of[n] = cur_phase
                out.protected[n] = (False if n.startswith("@")
                                    else default_protected(n))
                self.occupied.append((a, a + sz))
                out.log.append(f"autocall-backfill {n} @ {a:#07x} ({sz} hw)")

        def flush_rollin():
            for bank, _, name, size, ovl in sorted(
                    rollin_pool, key=lambda t: (t[0], t[3], t[1])):
                if name in out.addresses:
                    continue
                a = None
                for g_lo, g_hi in _bank_gaps(bank):
                    if g_hi - g_lo < 4:
                        continue          # a checksum slot, not a real gap
                    start = align_word(g_lo) if self.align else g_lo
                    if start + size <= g_hi:
                        a = start
                        break
                if a is None:
                    out.log.append(f"roll-in {name}: no free gap in bank "
                                   f"{bank}; left unplaced")
                    continue
                out.addresses[name] = a
                out.bank_of[name] = bank
                out.overlay_of[name] = ovl
                out.phase_of[name] = cur_phase
                out.protected[name] = default_protected(name)
                self.occupied.append((a, a + size))
                rollin_ends.add(a + size)
            rollin_pool.clear()
            # Deferred roll-in blocks: whole-block first-fit into the free
            # gaps, after the pool has claimed its space.
            for bank, ovl, block in rollin_blocks:
                block = [(n, s) for n, s in block if n not in out.addresses]
                if not block:
                    continue
                unit = sum((align_word(s) if self.align else s)
                           for _, s in block)
                a = None
                for g_lo, g_hi in _bank_gaps(bank):
                    if g_hi - g_lo < 4:
                        continue
                    start = align_word(g_lo) if self.align else g_lo
                    if start + unit <= g_hi:
                        a = start
                        break
                if a is None:
                    out.log.append(f"roll-in block {ovl}: no free gap in "
                                   f"bank {bank}; left unplaced")
                    continue
                for n, s in block:
                    if self.align:
                        a = align_word(a)
                    out.addresses[n] = a
                    out.bank_of[n] = bank
                    out.overlay_of[n] = ovl
                    out.phase_of[n] = cur_phase
                    out.protected[n] = default_protected(n)
                    self.occupied.append((a, a + s))
                    a += s
                rollin_ends.add(a)
            rollin_blocks.clear()

        # INTER-BLOCK CHECKSUMS.  The LE writes a 2-halfword, fullword-
        # aligned CHECKSUM after every OVERLAY's placed content.  We model it
        # as a 2-hw `occupied` reservation right after the overlay's content:
        # downstream placement then flows past it via `_next_free`.  We
        # deliberately do NOT nudge `lc`/`banklc`: the block allocator floors
        # on `app_floor`/`banklc` (content ends, checksum-free) and lets
        # `_next_free` step over the reservations, so the checksums shift
        # content without perturbing the whole-overlay bookkeeping.
        # `cur_overlay_max` tracks the top of the current overlay's content
        # (inserts + any @-stacks that land in it); `close_overlay` emits the
        # reservation when the NEXT OVERLAY/BANK/PHASE card opens -- never on
        # STACK/SET/CLEAR, so the resident PDE chain keeps its trailing
        # stacks in the same block (one shared checksum).
        cur_overlay_max: int | None = None

        # ORPHAN PROGRAM-CODE FLUSH state: at end of job, program modules whose
        # $0<prog> is STACK-carded but never INSERTed get their code csects
        # ($0 + A*/B* blocks; #E/#D were placed by their own INSERTs) appended
        # after the checksum of the overlay that most recently placed a #C
        # (HAL procedure-code) csect in DECK order.
        orphan_floor: 'int | None' = None
        orphan_ctx: tuple = (None, None)     # (overlay, phase) of that floor
        overlay_had_c = False

        # AUTOCALL state: csect names owned by autocalled modules bypass the
        # legacy orphan flush and the STACK bank-0 fallback -- the autocall
        # waves below place them.  initial_occupied (the MAP imports, before
        # any placement) anchors the code wave's bank floor.
        autocall_units_ = list(autocall_units or [])
        autocall_names: set = set()
        autocall_stems: set = set()
        for stem_, sds_ in autocall_units_:
            autocall_stems.add(stem_)
            autocall_names.update(n for n, _ in sds_)
            # the linker GENERATES @0 stacks later -- they are not module
            # SDs yet, but the STACK-card fallback must leave them to wave1
            autocall_names.update("@0" + n[2:] for n, _ in sds_
                                  if n.startswith(("#E", "$0")))
        # A per-MC pins section applies when autocall is live (gate-decided
        # autocall.json) and the deck INSERTs the section's anchor csect.
        # A section with `streams` places the autocall surface as per-bank
        # streams; otherwise the wave placement below uses its pins.  Names
        # the section claims join autocall_names so the legacy orphan flush
        # / STACK bank-0 fallback leave them alone (and never leak into
        # NON-autocall jobs, where no wave would place them).
        pins = self.link_order
        mc = None
        if autocall_units_:
            inserts = {o.operand for o in program
                       if o.verb == "INSERT" and o.operand}
            mc = pins.mc_for(inserts)
        stream_job = mc is not None and bool(mc.streams)
        if stream_job:
            for names_ in mc.streams.values():
                autocall_names.update(names_)
        elif mc is not None:
            autocall_names.update(mc.preblock)
        initial_occupied = sorted(self.occupied)

        def register(nm, a, sz):
            nonlocal cur_overlay_max, overlay_had_c
            out.addresses[nm] = a
            out.bank_of[nm] = cur_bank
            out.overlay_of[nm] = cur_overlay
            out.phase_of[nm] = cur_phase
            unmarked.append(nm)
            self.occupied.append((a, a + sz))
            if nm.startswith("#C"):
                overlay_had_c = True
            cur_overlay_max = a + sz if cur_overlay_max is None \
                else max(cur_overlay_max, a + sz)
            # Queue this module's @0<M> stack to ride at the tail of the overlay
            # that placed its PDE #E<M> (see stack_decls above).
            if nm.startswith("#E"):
                stk = "@0" + nm[2:]
                if stk in stack_decls and stk not in out.addresses \
                        and stk not in stack_queue:
                    stack_queue.append(stk)

        def close_overlay():
            """Lay the overlay's deferred @0 stacks at the block tail, then
            reserve its trailing 2-hw checksum, then reset.

            Stacks are placed occupancy-driven via `register` (no manual
            `lc`/`banklc` nudge): the next overlay flushes at `banklc`/`app_floor`
            and `_next_free` steps over the stacks + checksum on its own.  The Z1
            magic ZCON pool is checksum-exempt: its pool is placed by the linker
            into a fixed region (Z1_POOL_END_HW) and conlayout sees only the lone
            INSERTed #ZFIOCGR, so a checksum here would land at the wrong address
            -- and the fixed pool region absorbs it regardless."""
            nonlocal cur_overlay_max
            for stk in stack_queue:                 # #E-INSERT order
                if stk in out.addresses:
                    continue
                ssz = stack_decls[stk]
                ex = self.existing.get(stk)
                if ex is not None:
                    a = ex
                else:
                    a = align_word(cur_overlay_max) if self.align \
                        else cur_overlay_max
                    a = self._next_free(a, ssz)
                register(stk, a, ssz)
                # Stacks are runtime-written and never store-protected -- keep them
                # OUT of `unmarked` so a following SET card cannot flip the flag
                # (register appended it; the old STACK handler set addresses
                # directly and bypassed the SET/CLEAR sweep entirely).
                if unmarked and unmarked[-1] == stk:
                    unmarked.pop()
                out.protected[stk] = False
            stack_queue.clear()
            nonlocal orphan_floor, orphan_ctx, overlay_had_c
            if cur_overlay_max is not None and cur_overlay != Z1_MAGIC_OVERLAY:
                ck = align_word(cur_overlay_max)
                self.occupied.append((ck, ck + 2))
                if overlay_had_c:
                    orphan_floor = ck + 2
                    orphan_ctx = (cur_overlay, cur_phase)
            overlay_had_c = False
            cur_overlay_max = None

        flow_flushed: set = set()   # flow overlays that already flushed once

        def flush_block():
            """Place the buffered unpinned-overlay block as one contiguous unit."""
            nonlocal lc
            if not pending:
                return
            block = [(n, s) for n, s in pending if n not in out.addresses]
            pending.clear()
            if not block:
                return
            if cur_rollin_flow:
                # LATENT SPLIT: a card that flushes mid-overlay
                # (SET/CLEAR/STACK/pinned-INSERT/BANK) splits the flow into
                # independently-first-fit fragments -- the second can land
                # BELOW the first.  No current deck trips this; if one ever
                # does, the placement is suspect.  Loud, not silent.
                if cur_overlay in flow_flushed:
                    out.log.append(
                        f"roll-in flow {cur_overlay}: SECOND flush "
                        f"mid-overlay -- block split into independently "
                        f"placed fragments; verify against the DASS")
                flow_flushed.add(cur_overlay)
                # NO RE-SUPPLY PIN.  A base-defined csect RIDES the flowing
                # block at its deck position -- it is not pinned to the base
                # address.
            unit = sum((align_word(s) if self.align else s) for _, s in block)
            if cur_rollin_flow:
                # Post-BANK-group flow: ONE contiguous block per overlay,
                # deck INSERT order, first-fit over the free gaps of
                # `self.occupied` ONLY -- MAP'd intervals, own placements
                # (including re-opened-region content + its checksum), the
                # Z1 reserve.  NOT over `base_occupied`: occupancy is config-
                # dependent, and not-MAP'd base regions are real holes the
                # flow fills.  Because the WHOLE block is the allocation unit
                # it cannot backfill a small hole csect-by-csect.  (First-fit,
                # NOT the bank's last gap: MAP'd content high in the bank is
                # an island the flow passes under, not over.)
                a = None
                for g_lo, g_hi in _bank_gaps(cur_bank, include_base=False):
                    if g_hi - g_lo < 4:
                        continue          # a checksum slot, not a real gap
                    start = align_word(g_lo) if self.align else g_lo
                    if start + unit <= g_hi:
                        a = start
                        break
                if a is None:
                    out.log.append(f"roll-in flow {cur_overlay}: no free gap "
                                   f"in bank {cur_bank}; left unplaced")
                    return
                for n, s in block:
                    if self.align:
                        a = align_word(a)
                    register(n, a, s)
                    a += s
                if a > banklc.get(cur_bank, 0):
                    banklc[cur_bank] = a
                lc = max(lc, a)
                return
            base = app_floor.get(cur_bank, cur_bank * BANK_SIZE_HW)
            # A pure-DATA (#D) overlay BACKFILLS the lowest free gap of the bank's
            # application region (first-fit from the application floor).  A
            # PDE/mixed/code overlay does NOT backfill -- it is placed
            # monotonically at the bank's high-water mark (still flowing
            # AROUND the pinned islands as a unit, so a big PDE overlay skips
            # past them).
            is_data = all(n.startswith("#D") for n, _ in block)
            floor = base if is_data else max(base, banklc.get(cur_bank, base))
            a = self._next_free(floor, unit)
            for n, s in block:
                if self.align:
                    a = align_word(a)
                register(n, a, s)
                a += s
            if a > banklc.get(cur_bank, 0):
                banklc[cur_bank] = a
            lc = max(lc, a)

        def remote_siblings(name, sizes):
            """REMOTE#R (A<digit>...) csects of a module that have no INSERT of
            their own.  The linkage editor lays each module's per-bank REMOTE
            data CONTIGUOUSLY AFTER its #C compool.  When the phase INSERTs
            no #C for the module, the A-remotes instead ride the module's
            $0<mod> STACKVAL SD csect.  The #C anchor wins when both exist
            (the $0 path gates on `#C<mod> not INSERTed`); the module's
            #D/#X/#Z siblings are placed by their own INSERTs elsewhere."""
            if not sizes:
                return []
            if name.startswith("$0"):
                if ("#C" + name[2:]) in all_inserts:
                    return []              # Fix#1 rides these after the #C
            elif not name.startswith("#C"):
                return []
            rem = [(n, s) for n, s in sizes
                   if len(n) > 1 and n[0] == "A" and n[1].isdigit()
                   and n not in all_inserts]
            rem.sort(key=lambda t: (int(t[0][1]), t[0]))  # A1, A2, ... in order
            return rem

        def buffer_block(name, size, sizes):
            """Add the named csect + its Stage-A #D siblings (and, for a #C
            compool or $0 STACKVAL, its REMOTE#R A-siblings) to the pending
            block."""
            pending.append((name, size))
            seen = False
            for sib, ssz in (sizes or []):
                if sib == name:
                    seen = True
                    continue
                if not seen:
                    continue
                if not sib.startswith("#D") or sib in all_inserts:
                    break
                pending.append((sib, ssz))
            for sib, ssz in remote_siblings(name, sizes):
                pending.append((sib, ssz))

        def place_island(name, size, sizes):
            """Place a pinned csect (and its Stage-A #D siblings) individually at
            the running lc, registering each as occupied (an island)."""
            nonlocal lc

            def _place(nm, sz):
                nonlocal lc
                if nm in out.addresses:
                    return
                if self.align:
                    lc = align_word(lc)
                # pinned content overrides the promoted Z1 reserve (the deck
                # said WHERE; the reserve only fences unpinned flow)
                lc = self._next_free(lc, sz, ignore=z1_promoted)
                register(nm, lc, sz)
                lc += sz

            _place(name, size)
            seen_name = False
            for sib, ssz in (sizes or []):
                if sib == name:
                    seen_name = True
                    continue
                if not seen_name:
                    continue
                if not sib.startswith("#D") or sib in all_inserts:
                    break
                _place(sib, ssz)
            for sib, ssz in remote_siblings(name, sizes):
                _place(sib, ssz)

        for op in program:
            verb = op.verb
            if verb == "PHASE":
                flush_block()
                close_overlay()
                cur_phase = op.operand

            elif verb == "BANK":
                try:
                    n = int(op.operand)
                except ValueError:
                    out.log.append(f"BANK with non-numeric operand {op.operand!r}")
                    continue
                flush_block()
                close_overlay()
                if n != cur_bank:
                    cur_bank = n
                if n != lc // BANK_SIZE_HW:
                    lc = switch_lc(banklc.get(n, n * BANK_SIZE_HW))
                in_pinned = False       # a BANK card resumes NATURAL fill

            elif verb == "OVERLAY":
                flush_block()
                close_overlay()
                prev_overlay = cur_overlay
                cur_overlay = op.operand or cur_overlay
                cur_rollin = False
                cur_rollin_flow = False
                if op.origin is not None:          # absolute load address
                    if app_floor_pending:
                        app_floor.setdefault(cur_bank, lc)
                        app_floor_pending = False
                    lc = switch_lc(op.origin)
                    in_pinned = True
                elif prev_overlay == Z1_MAGIC_OVERLAY \
                        and op.operand != Z1_MAGIC_OVERLAY:
                    # Leaving the Z1 magic ZCON pool: skip past the fixed pool
                    # region the LE reserves.  The following DSES csects are
                    # POSITIONED there (islands), and the bank-0 application floor
                    # is captured just after them.
                    lc = switch_lc(Z1_POOL_END_HW)
                    in_pinned = True
                    app_floor_pending = True
                elif op.operand in self.phase_regions:
                    # Re-open an earlier-phase region for replacement content.
                    lc = switch_lc(self.phase_regions[op.operand])
                    in_pinned = True
                    seen_reopen = True
                elif seen_reopen and self.base_occupied:
                    if cur_overlay in flow_ovls:
                        # Member of a post-BANK new-overlay group whose bank
                        # holds a #D/#X/#Y overlay: whole block, deck INSERT
                        # order, occupied-only first-fit (see the census).
                        cur_rollin_flow = True
                    else:
                        if rollin_ovl_count.get(cur_bank, 0) == 2:
                            # the pool-vs-buffer count threshold is
                            # INTERPOLATED (no observed non-flow deck has
                            # exactly two new overlays in a bank): the first
                            # deck to land here is new evidence
                            out.log.append(
                                f"census: bank {cur_bank} has EXACTLY 2 new "
                                f"overlays -- unmeasured threshold region "
                                f"(pool chosen); verify against the DASS")
                        if rollin_ovl_count.get(cur_bank, 0) >= 2:
                            # A NEW overlay of a roll-in phase sharing its
                            # bank with other new overlays (none of them
                            # data): csects go to the per-bank smallest-first
                            # pool (see flush_rollin); in practice non-data
                            # csects fall through to buffer_block.
                            cur_rollin = True

            elif verb == "INCLUDE":
                # A library INCLUDE loads the member; PLACEMENT still belongs
                # to the deck's INSERT card when one names the csect.  Only an
                # INCLUDE of a csect that is NEVER INSERTed but IS already
                # defined (an earlier-phase MAP import) places here: it
                # REPLACES the definition at its existing address.  The
                # current overlay's extent (checksum) is NOT touched: the
                # content lives in another overlay's region.
                if op.operand in all_inserts or op.operand in out.addresses:
                    continue
                sizes = self.module_csects.get(op.operand)
                size = dict(sizes).get(op.operand) if sizes else None
                ex = self.existing.get(op.operand)
                if ex is None or size is None:
                    continue               # not a replacement; auto-placement
                out.addresses[op.operand] = ex
                out.bank_of[op.operand] = ex // BANK_SIZE_HW
                out.overlay_of[op.operand] = cur_overlay
                out.phase_of[op.operand] = cur_phase
                out.protected[op.operand] = default_protected(op.operand)
                self.occupied.append((ex, ex + size))

            elif verb == "INSERT":
                if op.origin is not None:          # location field pins the address
                    flush_block()
                    if app_floor_pending:
                        app_floor.setdefault(cur_bank, lc)
                        app_floor_pending = False
                    lc = switch_lc(op.origin)
                    in_pinned = True
                sizes = self.module_csects.get(op.operand)
                size = dict(sizes).get(op.operand) if sizes else None
                if size is None:
                    out.unknown_inserts.append(op.operand)
                    continue
                if cur_rollin and op.origin is None \
                        and op.operand not in self.existing:
                    # (a re-supply INSERT of a base-defined csect keeps the
                    # normal whole-block flow)
                    if op.operand.startswith(("#D", "#X", "#Y")):
                        # Only detached DATA csects size-pack; compool runs
                        # keep block grouping.
                        rollin_pool.append((cur_bank, len(rollin_pool),
                                            op.operand, size, cur_overlay))
                        continue
                    if any(p[0] == cur_bank for p in rollin_pool):
                        # A block in a bank the pool is packing: defer it so
                        # it cannot take the pool's fill point.  Blocks in
                        # pool-free banks keep the normal whole-block flow
                        # below.
                        if not rollin_blocks \
                                or rollin_blocks[-1][:2] != (cur_bank,
                                                             cur_overlay):
                            rollin_blocks.append((cur_bank, cur_overlay, []))
                        rollin_blocks[-1][2].append((op.operand, size))
                        continue
                if in_pinned:
                    # a pinned overlay/insert: island at the absolute address
                    place_island(op.operand, size, sizes)
                else:
                    # an unpinned overlay: buffer the csect (+ its Stage-A #D
                    # siblings) into the current whole-overlay block
                    buffer_block(op.operand, size, sizes)

            elif verb == "RESERVE":
                # The LE "RESERVE feature" (CR091094): allocate the csect at
                # the normal INSERT flow position but load NO text -- the MMU
                # load-block reserve bit (allocation only).  No siblings ride
                # a reserve.
                sizes = self.module_csects.get(op.operand)
                size = dict(sizes).get(op.operand) if sizes else None
                if size is None:
                    out.unknown_inserts.append(op.operand)
                    continue
                out.reserved.add(op.operand)
                pending.append((op.operand, size))

            elif verb in ("SET", "CLEAR"):
                flush_block()
                for n in unmarked:
                    out.protected[n] = (verb == "SET")
                unmarked = []

            elif verb == "STACK":
                # STACK $0<prog> names a stack CSECT the linker CREATES
                # (@0<prog>, sized by the call-chain algorithm).  When the
                # module's PDE #E<prog> is INSERTed, the LE puts @0<prog> at the
                # tail of THAT overlay's block -- handled by the deferred
                # stack_queue at close_overlay, so this card is a no-op here.
                # Otherwise (no #E<prog> INSERTed) fall back to allocating from
                # BANK 0's fill point regardless of the bank the card appears in.
                name = "@" + op.operand[1:] if op.operand[:1] in "$@" \
                    else op.operand
                if name in autocall_names:
                    continue                   # placed by the autocall waves
                if ("#E" + name[2:]) in all_inserts:
                    continue                   # deferred to its #E overlay's tail
                flush_block()
                sizes = self.module_csects.get(name)
                size = dict(sizes).get(name) if sizes else None
                if size is None:
                    out.log.append(f"STACK {op.operand}: no generated CSECT "
                                   f"{name}; not placed")
                elif name not in out.addresses:
                    in_bank0 = lc // BANK_SIZE_HW == 0
                    slc = lc if in_bank0 else banklc.get(0, 0)
                    if self.align:
                        slc = align_word(slc)
                    slc = self._next_free(slc, size)
                    out.addresses[name] = slc
                    out.bank_of[name] = 0
                    out.overlay_of[name] = cur_overlay
                    out.phase_of[name] = cur_phase
                    out.protected[name] = False    # stacks are runtime-written
                    # every placement path must register occupancy, or a
                    # later data-overlay backfill (_next_free consults only
                    # self.occupied) first-fits ON TOP of this stack
                    self.occupied.append((slc, slc + size))
                    slc += size
                    # A generated stack shares its OVERLAY's checksum block
                    # (the resident PDE chain and its trailing @0 stacks are
                    # one block, ck only after the last stack) -- so extend the
                    # overlay content top to cover a stack that lands in the
                    # current overlay.
                    cur_overlay_max = slc if cur_overlay_max is None \
                        else max(cur_overlay_max, slc)
                    if in_bank0:
                        lc = slc
                    else:
                        banklc[0] = slc

        flush_block()                      # any trailing unpinned overlay block
        close_overlay()                    # its trailing checksum
        flush_rollin()                     # roll-in pool: smallest-first pack

        # ORPHAN PROGRAM-CODE FLUSH (see state above + the orphanFlush pins).
        stacked = [o.operand for o in program
                   if o.verb == "STACK" and o.operand
                   and o.operand.startswith("$0")]

        def flush_orphans(floor, ctx):
            units = []
            for s0 in stacked:
                if s0 in autocall_names:
                    continue               # placed by the autocall code wave
                if s0 in out.addresses or s0 in all_inserts:
                    continue               # placed (or will be) by the deck
                sds = self.module_csects.get(s0)
                if not sds:
                    continue               # module not in this link
                unit = [(n, sz) for n, sz in sds
                        if not n.startswith(("#", "@"))
                        and n not in out.addresses]
                if unit:
                    units.append((s0, unit))
            if not units:
                return
            if floor is None:
                # no overlay placed a #C, so there is no flush point: the
                # units fall through to the linker's sector-rule auto-place
                out.log.append(
                    "orphan flush: no #C overlay floor; "
                    + ", ".join(s0 for s0, _ in units)
                    + " left to linker auto-placement")
                return
            order = (mc.orphanFlush if mc is not None and mc.orphanFlush
                     else pins.orphanFlush)
            rank = {n: i for i, n in enumerate(order)}
            units.sort(key=lambda u: (rank.get(u[0], len(rank)),
                                      stacked.index(u[0])))
            ovl, ph = ctx
            a = floor
            for s0, unit in units:
                # within a unit: the $0 program csect, then its A1..A9/B0..
                # block csects
                unit.sort(key=lambda t: (t[0] != s0, t[0]))
                for n, sz in unit:
                    a = self._next_free(align_word(a) if self.align else a, sz)
                    out.addresses[n] = a
                    out.bank_of[n] = a // BANK_SIZE_HW
                    out.overlay_of[n] = ovl
                    out.phase_of[n] = ph
                    out.protected[n] = default_protected(n)
                    self.occupied.append((a, a + sz))
                    out.log.append(f"orphan-flush {n} @ {a:#07x} ({sz} hw) "
                                   f"after {ovl}")
                    a += sz

        # In a wave autocall job the orphan program code is part of the same
        # automatic-library-call surface as the waves, so it continues the
        # code run rather than the last #C overlay -- deferred to just after
        # that run below.  Stream jobs place the whole surface themselves and
        # keep the legacy floor.
        wave_job = bool(autocall_units_) and not stream_job and mc is not None
        if autocall_units_ and not stream_job and mc is None:
            # No mc section means no pinned order and the legacy bank floor:
            # placement is likely wrong, so say so.
            out.log.append(
                "autocall job matched no mc pins section (no deck INSERT of "
                "any anchor): generic wave placement + legacy orphan floor")
        if not wave_job:
            flush_orphans(orphan_floor, orphan_ctx)

        # ---- MC AUTOCALL STREAMS  --------------------
        # The job's autocall surface (autocalled members + STACK-orphan
        # program code) places as ONE monotonic stream per bank: each csect
        # first-fits at/after the previous placement, skipping to the next
        # free gap of the deck layout when it does not fit -- and never
        # backfilling.  Gaps are over `self.occupied` ONLY (MAP'd intervals,
        # own placements, Z1 reserve, checksum slots), same occupancy model
        # as the roll-in flow.
        if stream_job:
            sizeof: dict = {}
            for sds in self.module_csects.values():
                for n, sz in sds:
                    sizeof.setdefault(n, sz)
            for _, sds in autocall_units_:
                for n, sz in sds:
                    sizeof.setdefault(n, sz)
            for bank in sorted(mc.streams):
                gaps = _bank_gaps(bank, include_base=False)
                gi = 0
                pos = gaps[0][0] if gaps else bank * BANK_SIZE_HW
                if bank != 0:
                    # Code/compool banks: the stream starts at the bank's
                    # deck fill point (after the deck content + its closing
                    # checksum) -- it does NOT backfill the deck's interior
                    # gaps.  Only bank 0's data stream backfills.
                    #
                    # The fill point is inferred from the job's own content,
                    # which is wrong when the deck also places a bank-END
                    # marker at the top of the bank: the inferred floor lands
                    # above every gap.  A pinned floor wins where the section
                    # supplies one.
                    floor = mc.floors.get(bank)
                    if floor is None:
                        floor = max((a + (sizeof.get(n) or 0)
                                     for n, a in out.addresses.items()
                                     if a // BANK_SIZE_HW == bank),
                                    default=bank * BANK_SIZE_HW)
                    while gi < len(gaps) and gaps[gi][1] <= floor:
                        gi += 1
                    if gi < len(gaps):
                        pos = max(gaps[gi][0], floor)
                for n in mc.streams[bank]:
                    if n == "+2":
                        # a pinned 2-hw slot (checksum-slot family) between
                        # stream members
                        if gi < len(gaps):
                            lo, hi = gaps[gi]
                            start = align_word(max(lo, pos)) if self.align \
                                else max(lo, pos)
                            if start + 2 <= hi:
                                self.occupied.append((start, start + 2))
                                pos = start + 2
                        continue
                    # NOTE: no `self.existing` skip -- the stream places
                    # this job's own copies even when a base MAP defines the
                    # name elsewhere.
                    if n in out.addresses or n in all_inserts:
                        continue
                    # ...but a stream member this job does not have a copy of 
                    # is a different animal: it is defined only by a MAP'd base
                    # module, the LE resolved it there and allocated nothing.
                    # What doesn't get discarded is the `occupied` interval
                    # so placing it burns bank space that the deck's blocks
                    # and the rest of the stream then flow around.
                    if self.local_defs is not None \
                            and n not in self.local_defs \
                            and n in self.existing:
                        out.log.append(f"autocall-stream {n}: base-only "
                                       f"(no copy in this link); resolved to "
                                       f"{self.existing[n]:#07x}, not placed")
                        continue
                    sz = sizeof.get(n)
                    if sz is None:
                        continue           # module not in this link
                    a = None
                    while gi < len(gaps):
                        lo, hi = gaps[gi]
                        start = align_word(max(lo, pos)) if self.align \
                            else max(lo, pos)
                        if start + sz <= hi:
                            a = start
                            break
                        gi += 1
                        if gi < len(gaps):
                            pos = gaps[gi][0]
                    if a is None:
                        out.log.append(f"autocall-stream {n}: no gap left in "
                                       f"bank {bank}; left to linker "
                                       f"auto-placement")
                        continue
                    out.addresses[n] = a
                    out.bank_of[n] = bank
                    out.overlay_of[n] = cur_overlay
                    out.phase_of[n] = cur_phase
                    out.protected[n] = (False if n.startswith("@")
                                        else default_protected(n))
                    self.occupied.append((a, a + sz))
                    out.log.append(f"autocall-stream {n} @ {a:#07x} ({sz} hw) "
                                   f"bank {bank}")
                    pos = a + sz

        # Stream leftovers: autocall data-zone csects not in a stream fall
        # through to the linker's append-at-top auto-placement, but we 
        # need to backfill them.
        if stream_job:
            leftovers = [n for n in autocall_names
                         if n not in out.addresses and n not in all_inserts
                         and n.startswith(("#D", "#P", "#X", "#E"))]
            backfill_leftovers(leftovers, sizeof)
            rest = [n for n in autocall_names
                    if n not in out.addresses and n not in all_inserts
                    and not n.startswith(("#Z", "#Q"))]
            backfill_leftovers(rest, sizeof, banks=range(8))

        # ---- AUTOCALL WAVES (preblock/wave1/compool/code pins) ------------
        if autocall_units_ and not stream_job:
            mcp = mc if mc is not None else McPins()
            sizeof: dict = {}
            for sds in self.module_csects.values():
                for n, sz in sds:
                    sizeof.setdefault(n, sz)
            for _, sds in autocall_units_:
                for n, sz in sds:
                    sizeof.setdefault(n, sz)

            def put(n, a, tag):
                sz = sizeof.get(n)
                if sz is None or n in out.addresses:
                    return a
                a = self._next_free(align_word(a) if self.align else a, sz)
                out.addresses[n] = a
                out.bank_of[n] = a // BANK_SIZE_HW
                out.overlay_of[n] = cur_overlay
                out.phase_of[n] = cur_phase
                out.protected[n] = (False if n.startswith("@")
                                    else default_protected(n))
                self.occupied.append((a, a + sz))
                out.log.append(f"autocall/{tag} {n} @ {a:#07x} ({sz} hw)")
                return a + sz

            stems_in_order = [s for s, _ in autocall_units_]
            unit_of = dict(autocall_units_)

            def pin_order(pins, names):
                rank = {n: i for i, n in enumerate(pins)}
                return sorted(names, key=lambda n: (rank.get(n, len(rank)),
                                                    names.index(n)))

            # PRE-BLOCK: rides after the checksum of the block that placed
            # preblockAfter (pinned).
            after = out.addresses.get(mcp.preblockAfter)
            if after is None:
                after = self.existing.get(mcp.preblockAfter)
            if after is not None:
                a = align_word(after + sizeof.get(mcp.preblockAfter, 0))
                a += 2                                   # that block's checksum
                for n in mcp.preblock:
                    a = put(n, a, "preblock")
                rollin_ends.add(a)

            # BANK-0 TAIL: one contiguous flow at the fill point after the
            # deck's trailing @-stacks.  wave1: PDE+stack of autocalled
            # programs / #D of autocalled procedures; wave2: leftover #D in
            # deck-card order; wave3: autocalled #P compools.
            tail = max((out.addresses[n] + sizeof.get(n, 0)
                        for n in out.addresses
                        if n.startswith("@0") and out.addresses[n] < BANK_SIZE_HW),
                       default=banklc.get(0, 0))
            for stem in pin_order(mcp.wave1Order, stems_in_order):
                sds = unit_of.get(stem, [])
                names = [n for n, _ in sds]
                if any(n.startswith("$0") for n in names):   # program unit
                    for n in names:
                        if n.startswith("#E"):
                            tail = put(n, tail, "wave1")
                            tail = put("@0" + n[2:], tail, "wave1")
                else:                                        # procedure unit
                    for n in names:
                        if n.startswith("#D"):
                            tail = put(n, tail, "wave1")
            carded: list = []
            for op in program:
                if op.verb not in ("INSERT", "STACK") or not op.operand:
                    continue
                sds = self.module_csects.get(op.operand)
                if not sds:
                    continue
                key = tuple(n for n, _ in sds)
                if key not in carded:
                    carded.append(key)
            for key in carded:
                for n in key:
                    if n.startswith("#D") and n not in out.addresses:
                        tail = put(n, tail, "wave2")
            # the #D run and the compool block are separate protected
            # blocks: a 2-hw checksum sits between
            ck2 = align_word(tail)
            self.occupied.append((ck2, ck2 + 2))
            tail = ck2 + 2
            pools = [n for s in stems_in_order for n, _ in unit_of[s]
                     if n.startswith("#P") and n not in out.addresses]
            # Only pinned pools ride the wave3 tail run; an unpinned
            # autocalled pool is backfilled.
            pinned = [n for n in pools if n in mcp.compoolOrder]
            for n in pin_order(mcp.compoolOrder, pinned):
                tail = put(n, tail, "wave3")
            ck = align_word(tail)
            self.occupied.append((ck, ck + 2))           # tail block checksum
            backfill_leftovers(
                [n for n in pools if n not in mcp.compoolOrder], sizeof)

            # code run: after the MAP-imported base's content checksum in
            # the pinned codeBank (mappedIntervals already carry the +2 hw).
            lob = mcp.codeBank * BANK_SIZE_HW
            code = max((hi for lo, hi in initial_occupied
                        if lob <= lo < lob + BANK_SIZE_HW), default=lob)
            for stem in pin_order(mcp.codeOrder, stems_in_order):
                names = [n for n, _ in unit_of.get(stem, [])]
                names.sort(key=lambda n: (not n.startswith("$0"),
                                          not n[:1] in "AB", n))
                for n in names:
                    if n.startswith(("$0", "#C")) or (n[:1] in "AB"
                                                      and len(n) == 8):
                        code = put(n, code, "code")

            # the orphan program code continues the code run as one block:
            # word-aligned, with no checksum slot between 
            if wave_job:
                flush_orphans(align_word(code), (cur_overlay, cur_phase))

        for n in unmarked:                 # trailing block with no mark card
            out.protected[n] = default_protected(n)
        return out
