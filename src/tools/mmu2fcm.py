#!/usr/bin/env python3
"""mmu2fcm -- emulate the GPC IPL/SSL load, composing a memory-configuration
image from the linked phase load modules (.lib).

The flight loader chain (GPCIPL -> FCMBOOT -> FCMINSSL/FCMMOVE/FCMINMMR)
reads the MMU phase table and places each phase's load blocks at their GPC
main-memory addresses, in IPL order, later phases overlaying earlier ones
(FCOS overlay).  Our load blocks ARE the .lib TEXT extents, already at
absolute linked addresses, so the composition reads the .lib files
directly.

Memory not covered by any load block keeps the IPL hardware background fill:
C9FB in hw 0x00000-0x1FFFF, C6C6 in hw 0x20000-0x7FFFF (banks 0-3 vs 4-15).
Alloc-only csects (the load-block RESERVE bit -- no data on tape) have no
TEXT extent and correctly stay as background fill.

Each checksum-group run (TEXT extent) is followed on tape by its 2-hw
fullword-aligned CHECKSUM slot, INCLUDED in the load block (mmbstamp rule:
lb_len = roundup_fullword(extent_end) - extent_start + 2, capped at the
bank boundary).  The MMB stages tape blocks from an INIT=C6C6 buffer, so an
unstamped slot loads as C6C6 -- NOT the IPL background (the flight DASS
shows C6C6 at every unstamped slot below hw 0x20000, where the IPL
background would be C9FB).  compose() therefore fills each extent's
trailing pad+slot halfwords with C6C6 wherever no phase's TEXT owns them.

Output is a phase-dir-shaped pair <out>/<NAME>.fcm + <NAME>.sym.json (the
union of the constituent phases' sym.json, overlay-aware).
"""
import argparse
import json
import logging
import sys
from pathlib import Path

from ap101Utils.concard import ConcardDeck, layout_program, phase_numbers
from ap101Utils.libModule import LibModule
from ap101Utils.mcconfigs import (CONFIG_NAMES, McConfigError,
                                  listConfigs, load_configs)
from ap101Utils.objModule import EsdType
from ap101Utils import psaIdent

log = logging.getLogger("mmu2fcm")

MEM_HW = 0x80000                     # 512K halfwords of addressable memory
FILL_LOW, FILL_HIGH = b"\xC9\xFB", b"\xC6\xC6"
FILL_SPLIT_HW = 0x20000              # C9FB below, C6C6 at/above
BANK_HW = 0x8000                     # 32K-hw bank; a checksum slot never
                                     # crosses into the next bank
SLOT_FILL = b"\xC6\xC6"              # MMB INIT=C6C6 staging buffer


def phaseName(n: int) -> str:
    return f"PHASE{n:02d}"


def loadPhase(mmuRoot: Path, n: int):
    """Return (name, LibModule, symJson) for phase n under the con80build
    output layout: <mmu>/PHASEnn.lib + <mmu>/PHASEnn/PHASEnn.sym.json."""
    name = phaseName(n)
    libPath = mmuRoot / f"{name}.lib"
    symPath = mmuRoot / name / f"{name}.sym.json"
    lib = LibModule.read(libPath)
    sym = json.load(open(symPath))
    if not lib.crossResolved:
        # the flag is only set when resolution patched something; a phase
        # with no external references legitimately never gets it
        ers = sum(1 for r in lib.rlds
                  if (e := lib.cesdEntry(r.relId)) and e.type == EsdType.ER)
        if ers:
            log.warning(f"{libPath}: {ers} external-reference RLD sites not "
                        f"cross-phase resolved -- compose will carry their "
                        f"addends (run lnk101 --resolve-phases)")
    return name, lib, sym


def iplMfbRegions(deck: ConcardDeck, phase: int):
    """The GNC-decouple cut (CR90717 OI25.04): the IPL image stages the
    MFB phase's load blocks only for the regions its structure member --
    the deck member carrying the phase's BANK cards (MFB3) -- declares
    BEFORE the first BANK card (IM1IH1 "ALL MC'S", FCOSFIO1 "SET UP IN
    PHASE 2").  Everything banked is OPS-transition content (MFB3: "THE
    UPPER MEMORY LOAD BLOCKS MUST BE LAST IN THE PHASE"), as are the
    phase's patch space and Z-pool contribution, which come from other
    members (PATCH03, PHASE03).  DASS_SSW is empty at exactly these
    regions, while G2/G16/G8/G9 carry them at identical addresses.

    Returns ([regionName, ...], structureMember); ([], None) when the
    phase's segment has no BANK card (no cut)."""
    seg, pre = None, []
    for op in layout_program(deck):
        if op.verb == "PHASE":
            nums = phase_numbers(op.operand)
            seg = nums[0] if nums else None
            continue
        if seg != phase:
            continue
        if op.verb == "BANK":
            return [name for name, card in pre if card == op.card], op.card
        if op.verb == "OVERLAY":
            pre.append((op.operand, op.card))
    return [], None


def reservedByPhase(deck: ConcardDeck) -> dict[int, list[str]]:
    """RESERVE-carded csects per phase segment (CON80 OPS0: `RESERVE
    #PCVNMMU`).  The link allocates without text, but the MMB still
    stages the allocation as a ZEROED tape block: DASS_SSW walks
    #PCVNMMU's body as UNSTARRED zeros and puts a checksum slot right
    after its end (0x3432C) -- both only possible if a block was staged.
    The listing brackets such csects `*** BEGIN/END RESERVED CSECT ***`."""
    out: dict[int, list[str]] = {}
    if not deck.has("OFTMP"):     # no layout program -> no RESERVE cards
        return out
    seg = None
    for op in layout_program(deck):
        if op.verb == "PHASE":
            nums = phase_numbers(op.operand)
            seg = nums[0] if nums else None
        elif op.verb == "RESERVE" and seg is not None:
            out.setdefault(seg, []).append(op.operand.strip())
    return out


def compose(mmuRoot: Path, phases: list[int], iplCut=None, deck=None):
    """IPL fill + overlay each phase's TEXT extents in load order.
    Returns (image bytes, owner bytearray, per-phase info list).
    owner[hw] = 0 for background fill, else 1-based index into phases.

    iplCut = (phaseNum, [regionNames]): stage only that phase's extents
    intersecting the named regions' REGN intervals (the IPL image's
    GNC-decouple cut -- see iplMfbRegions)."""
    image = bytearray(FILL_LOW * FILL_SPLIT_HW +
                      FILL_HIGH * (MEM_HW - FILL_SPLIT_HW))
    owner = bytearray(MEM_HW)
    info = []
    for idx, n in enumerate(phases, start=1):
        name, lib, sym = loadPhase(mmuRoot, n)
        rangesHW = None
        if iplCut and n == iplCut[0]:
            ivals = [(r.start, r.end) for r in lib.regions
                     if r.name in iplCut[1]]
            missing = set(iplCut[1]) - {r.name for r in lib.regions}
            if missing:
                log.warning(f"{name}: no REGN record for IPL-staged "
                            f"region(s) {sorted(missing)}")
            kept = [x for x in lib.extents
                    if any(x.address < e and x.address + len(x.data) > s
                           for s, e in ivals)]
            cut = len(lib.extents) - len(kept)
            cutHW = sum(len(x.data) for x in lib.extents
                        if x not in kept) // 2
            lib.extents = kept
            rangesHW = [[s // 2, e // 2] for s, e in sorted(ivals)]
            log.info(f"{name}: IPL cut -- staging only "
                     f"{'/'.join(iplCut[1])}; {cut} extents ({cutHW} hw) "
                     f"deferred to OPS transition")
        placed = overlaid = 0
        for x in lib.extents:
            if x.address % 2 or x.address + len(x.data) > 2 * MEM_HW:
                raise SystemExit(f"{name}: extent 0x{x.address:06X} "
                                 f"len {len(x.data)} outside/unaligned")
            hw0, nhw = x.address // 2, len(x.data) // 2
            overlaid += sum(1 for i in range(hw0, hw0 + nhw) if owner[i])
            image[x.address:x.address + len(x.data)] = x.data
            for i in range(hw0, hw0 + nhw):
                owner[i] = idx
            placed += nhw
        info.append(dict(index=idx, phase=n, name=name, lib=lib, sym=sym,
                         placedHW=placed, overlaidHW=overlaid,
                         loadRangesHW=rangesHW))
        log.info(f"{name}: {len(lib.extents)} extents, {placed} hw placed"
                 + (f", {overlaid} hw overlay earlier phases" if overlaid
                    else ""))

    # RESERVE-carded csects: stage the zeroed MMB block (see
    # reservedByPhase) over background/boot residue -- later phases'
    # real text wins.  Runs before the display load so the reserve's
    # space reads occupied to its allocator, and before the slot pass
    # so the block's trailing checksum slot appears (DASS 0x3432C).
    if deck is not None and info:
        bootRes = info[0]["index"]
        resv = reservedByPhase(deck)
        for ph in info:
            for nm in resv.get(ph["phase"], []):
                sec = next((s for s in ph["sym"].get("sections", [])
                            if s["name"] == nm
                            and not s.get("module", "").endswith(">")), None)
                if sec is None:
                    continue
                a, n = sec["address"], sec["size"]
                zeroed = 0
                for i in range(a, min(a + n, MEM_HW)):
                    if owner[i] in (0, bootRes):
                        image[2 * i:2 * i + 2] = b"\x00\x00"
                        owner[i] = ph["index"]
                        zeroed += 1
                sec["reserved"] = True
                ph.setdefault("extentsHW", []).append([a, n])
                log.info(f"RESERVE: {nm} staged zeroed "
                         f"({zeroed}/{n} hw) at {a:#07x}")

    # the SSW/OPS0 display roll-in: its blocks land in the holes the cut
    # leaves, so it runs only with the cut -- and before the slot pass, so
    # its staged blocks get their trailing checksum slots
    ops0 = None
    if iplCut:
        ops0 = ops0DisplayLoad(mmuRoot, image, owner, info, phases[0])

    # CHECKSUM slots: the tape block staged for each extent run carries the
    # trailing fullword-aligned 2-hw slot (plus the odd-end pad halfword)
    # from the MMB's INIT=C6C6 buffer.  Fill them wherever no LATER phase's
    # TEXT claimed the bytes -- the boot phase's residue is dead memory a
    # staged slot overwrites just like staged text (DASS: the display
    # load's 0x5E0/0x4D38 slots sit inside GPCIPL's extent).  Run after
    # ALL phases so a slot halfword supplied as real content by a later
    # phase is never clobbered.
    bootIdx = info[0]["index"] if info else 0
    slot_hw = set()
    for ph in info:
        if ph["index"] == bootIdx:
            continue          # boot-block tails live in unlisted holes
        ends = ([(x.address + len(x.data)) // 2 for x in ph["lib"].extents]
                if ph["lib"] is not None else [])
        ends += [a + n for a, n in ph.get("extentsHW", [])]
        for end in ends:
            cap = ((end - 1) // BANK_HW + 1) * BANK_HW
            for i in range(end, min((end + 1) // 2 * 2 + 2, cap, MEM_HW)):
                if owner[i] in (0, bootIdx):
                    image[2 * i:2 * i + 2] = SLOT_FILL
                    slot_hw.add(i)
    if slot_hw:
        log.info(f"checksum slots: {len(slot_hw)} background hw filled C6C6")
    return bytes(image), owner, info, ops0


def ops0DisplayLoad(mmuRoot: Path, image: bytearray, owner: bytearray,
                    info: list[dict], bootPhase: int) -> dict | None:
    """Stage the SSW/OPS0 display roll-in (DASS_SSW 0x5CE/0x4C70 families).

    The base keeps the display SPEC processors' CODE resident (#CDSPSPC,
    #CDPDSPC, #CDXRDMM, #CDXCCCS ride PHASE02's SMDSPPRO/ROLLINPR/CARGOPRO
    regions in every config) but their #D/#X data blocks ship with the SM
    MFB.  Post-IPL, the DMM stages those blocks from the PHASE14 members
    into free memory -- content VERBATIM (adcons keep their phase-14-link
    values; DASS_SSW #DDSPSPC's pool words point at 0x3BBC-family S2
    addresses), placed smallest-first / deck-order ties / fullword-aligned
    first-fit over the config-map free space: the IPL phase's residue is
    dead memory (0x5CE sits inside GPCIPL), the Z-pool region is reserved,
    prospective checksum slots are not free.  The six placements land at
    DASS_SSW's 0x5CE/0x5D0/0x5D2 and 0x4C70/0x4CAA/0x4CE4.

    Mutates image/owner and appends a pseudo-phase entry to info; returns
    the repro record ({name: [addrHW, sizeHW]}, extents) or None."""
    # data-less display processors of the composed base
    have_c, have_d = set(), set()
    for ph in info:
        for s in ph["sym"].get("sections", []):
            if s.get("module", "").endswith(">"):
                continue
            if s["name"].startswith("#C"):
                have_c.add(s["name"][2:])
            elif s["name"].startswith("#D"):
                have_d.add(s["name"][2:])
    stems = have_c - have_d
    if not stems:
        return None
    try:
        _, lib14, sym14 = loadPhase(mmuRoot, 14)
    except FileNotFoundError as e:
        log.warning(f"OPS0 display load skipped -- {e}")
        return None
    secs14 = {s["name"]: s for s in sym14.get("sections", [])
              if not s.get("module", "").endswith(">")}
    items = []
    for stem in stems:
        d = secs14.get("#D" + stem)
        if d is None:
            continue                 # no SM-MFB data block (e.g. FIOCGR)
        items.append(d)
        x = secs14.get("#X" + stem)
        if x is not None:
            items.append(x)
    if not items:
        return None
    items.sort(key=lambda s: (s["size"], s["address"]))

    # config-map free space: the IPL phase's residue counts as free, the
    # Z-pool reserve and the prospective slot halfwords do not.  The pool
    # reserve spans from the Z1 region origin to the first non-pool csect
    # after it ($ZDSECLR): the base link holds the whole span for every
    # config's zcon ordinals even though the IPL set only fills its prefix.
    bootIdx = {ph["index"] for ph in info if ph["phase"] == bootPhase}
    z1 = min((r.start // 2 for ph in info if ph["lib"] is not None
              for r in ph["lib"].regions if r.name == "Z1"), default=None)
    zres = (0, 0)
    if z1 is not None:
        after = [s["address"] for ph in info
                 for s in ph["sym"].get("sections", [])
                 if s["address"] >= z1
                 and not s["name"].startswith(("#Z", "#Q"))
                 and not s.get("module", "").endswith(">")]
        zres = (z1, min(after)) if after else (z1, z1)
    slots = set()
    for ph in info:
        if ph["index"] in bootIdx:
            continue
        for x in ph["lib"].extents:
            end = (x.address + len(x.data)) // 2
            cap = ((end - 1) // BANK_HW + 1) * BANK_HW
            for i in range(end, min((end + 1) // 2 * 2 + 2, cap, MEM_HW)):
                if owner[i] == 0 or owner[i] in bootIdx:
                    slots.add(i)

    def free(i):
        return ((owner[i] == 0 or owner[i] in bootIdx) and i not in slots
                and not zres[0] <= i < zres[1])

    gaps, s = [], None
    for i in range(MEM_HW):
        if free(i):
            if s is None:
                s = i
        elif s is not None:
            gaps.append([s, i - s])
            s = None
    if s is not None:
        gaps.append([s, MEM_HW - s])

    placedSecs, extents = {}, []
    for it in items:
        size = it["size"]
        for g in gaps:
            if g[1] >= size:
                a = g[0]
                g[0] += size
                g[1] -= size
                if g[0] % 2 and g[1] > 0:      # fullword-align the next
                    g[0] += 1
                    g[1] -= 1
                break
        else:
            log.warning(f"OPS0 display load: no gap fits {it['name']} "
                        f"({size} hw) -- skipped")
            continue
        src = it["address"]
        data = bytearray(2 * size)             # EXCLUSIVEs carry no text
        for x in lib14.extents:
            lo = max(x.address, 2 * src)
            hi = min(x.address + len(x.data), 2 * (src + size))
            if lo < hi:
                data[lo - 2 * src:hi - 2 * src] = \
                    x.data[lo - x.address:hi - x.address]
        image[2 * a:2 * a + len(data)] = data
        idx = len(info) + 1
        for i in range(a, a + size):
            owner[i] = idx
        placedSecs[it["name"]] = [a, size, it.get("module")]
        extents.append([a, size])
        log.info(f"OPS0 display load: {it['name']} ({size} hw) "
                 f"PHASE14 {src:#07x} -> {a:#07x}")

    # pseudo-phase entry so unionSym carries the sections/symbols; the
    # symbol addresses rebase by each section's move delta
    delta = {n: placedSecs[n][0] - secs14[n]["address"] for n in placedSecs}

    # the stager relocates adcons whose RLD target is within the moved
    # family (self-pointers, #D -> its #X slot: DASS_SSW #DDSPSPC+1C =
    # 05CE, the moved slot); external targets keep their phase-14-link
    # values (#DDSPSPC+0 stays 3BB8-family)
    def home(hw):
        for n, (a, size, _m) in placedSecs.items():
            src = secs14[n]["address"]
            if src <= hw < src + size:
                return n
        return None

    fixed = 0
    for r in sym14.get("relocations", []):
        sec = home(r["address"])
        if sec is None or r.get("targetName") not in delta:
            continue
        site = r["address"] + delta[sec]
        val = (image[2 * site] << 8) | image[2 * site + 1]
        val = (val + delta[r["targetName"]]) & 0xFFFF
        image[2 * site:2 * site + 2] = val.to_bytes(2, "big")
        fixed += 1
    if fixed:
        log.info(f"OPS0 display load: {fixed} family-internal adcons "
                 f"relocated to the staged addresses")
    sym = {"sections": [dict(secs14[n], address=placedSecs[n][0])
                        for n in placedSecs],
           "symbols": [], "modules": [], "relocations": [],
           "unresolvedRelocations": []}
    for e in sym14.get("symbols", []):
        for n, (a, size, _m) in placedSecs.items():
            src = secs14[n]["address"]
            if src <= e["address"] < src + size:
                sym["symbols"].append(dict(e, address=e["address"] + delta[n]))
                break
    seen = {m["name"] for ph in info for m in ph["sym"].get("modules", [])}
    sym["modules"] = [m for m in sym14.get("modules", [])
                      if m["name"] in {v[2] for v in placedSecs.values()}
                      and m["name"] not in seen]
    info.append(dict(index=len(info) + 1, phase=None, name="OPS0DSP",
                     lib=None, sym=sym, placedHW=sum(n for _, n in extents),
                     overlaidHW=0, loadRangesHW=None, extentsHW=extents))
    return {"sections": {n: v[:2] for n, v in placedSecs.items()},
            "extentsHW": extents}


def reportOverlays(image: bytes, owner: bytearray, info: list[dict]) -> int:
    """Log every text-bearing section a later phase overwrote with DIFFERENT
    bytes.  Same-name re-supply and identical-byte re-supply (the Z1 ZCON
    pool) are silent, as is anything a later phase replaces with the same
    content; what survives is (a) the by-design SSL->FCOS replacement of the
    boot phase and (b) different csects colliding."""
    damaged = 0
    for ph in info:
        if ph["index"] == len(info):
            break
        names = {s["name"] for p in info if p["index"] > ph["index"]
                 for s in p["sym"].get("sections", [])
                 if not s.get("module", "").endswith(">")}

        def ownBytes(hw):     # this phase's own text at halfword hw, or None
            b = 2 * hw
            for x in ph["lib"].extents:
                if x.address <= b < x.address + len(x.data):
                    return x.data[b - x.address:b - x.address + 2]
            return None

        for s in ph["sym"].get("sections", []):
            if s.get("module", "").endswith(">") or s["name"] in names:
                continue          # imported, or same-name re-supply by design
            a, n = s["address"], s["size"]
            lost = diff = 0
            by = set()
            for i in range(a, min(a + n, MEM_HW)):
                if owner[i] <= ph["index"]:
                    continue
                lost += 1
                mine = ownBytes(i)
                if mine is not None and mine != image[2 * i:2 * i + 2]:
                    diff += 1
                    by.add(info[owner[i] - 1]["name"])
            if not diff:
                continue          # intact, or replaced with identical bytes
            damaged += 1
            log.warning(f"collision: {ph['name']} {s['name']} @hw {a:#07x} "
                        f"loses {lost}/{n} hw to {'/'.join(sorted(by))} "
                        f"({diff} hw byte-differ)")
    return damaged


def unionSym(cfgName: str, owner: bytearray, info: list[dict],
             ops0: dict | None = None) -> dict:
    """Merge the constituent phases' sym.json overlay-aware:
    - sections/symbols: entries whose module carries text (no '>' marker)
      beat imported markers; among text-bearing duplicates the later-loaded
      phase wins (overlay semantics).
    - relocations / unresolvedRelocations: kept only if the fixup site still
      belongs to that phase in the composed image (not overlaid later).
    - modules: union by name."""
    def imported(entry):
        return entry.get("module", "").endswith(">")

    def staged(ph, e):
        """False for a truncated phase's own entry wholly outside its
        IPL-staged ranges (its text was never loaded -- see iplMfbRegions).
        Imported markers describe other phases' space and pass through."""
        rng = ph.get("loadRangesHW")
        if rng is None or imported(e):
            return True
        a, n = e["address"], max(e.get("size", 1), 1)
        return any(a < end and a + n > start for start, end in rng)

    def shadowed(ph, e):
        """True for a section whose own TEXT a different-named later
        phase's text fully overwrote: flight maps list only the overlay
        winner (DASS_SSW has MFBZFILL not $X030001 at 0x654; G2 the
        reverse; neither lists PHASE02's 1-hw #PCVMS8C under PHASE13).
        Text-less sections (stacks, RESERVEs) always survive."""
        if imported(e) or ph["lib"] is None:
            return False
        a, n = e["address"], max(e.get("size", 1), 1)
        # a staged RESERVE counts as text: an MC whose content covers the
        # whole allocation lists its own csects there, not the reserve
        had = e.get("reserved") or any(
            x.address < 2 * (a + n) and x.address + len(x.data) > 2 * a
            for x in ph["lib"].extents)
        return had and all(owner[i] != ph["index"]
                           for i in range(a, min(a + n, MEM_HW)))

    def mergeNamed(key):
        out = {}
        dropped = 0
        shadow: set[str] = set()
        for ph in info:
            for e in ph["sym"].get(key, []):
                if not staged(ph, e):
                    dropped += 1
                    continue
                if key == "sections" and shadowed(ph, e):
                    shadow.add(e["name"])
                    continue
                cur = out.get(e["name"])
                if cur is None or imported(cur[1]) or not imported(e):
                    out[e["name"]] = (ph["index"], e)
        # an imported marker must not resurrect a shadow-dropped name --
        # it points at content the later phase overwrote
        for name in shadow:
            if name in out and imported(out[name][1]):
                del out[name]
        if dropped:
            log.info(f"IPL cut: {dropped} {key} outside the staged ranges "
                     f"dropped")
        if shadow:
            log.info(f"{len(shadow)} sections fully overlaid by later "
                     f"phases dropped (flight maps list the winner)")
        return [e for _, e in out.values()]

    sections = mergeNamed("sections")
    symbols = mergeNamed("symbols")

    modules, seen = [], set()
    relocations, unresolved = [], []
    droppedRel = droppedUnres = 0
    for ph in info:
        for m in ph["sym"].get("modules", []):
            if m["name"] not in seen:
                seen.add(m["name"])
                modules.append(m)
        for r in ph["sym"].get("relocations", []):
            if owner[r["address"]] == ph["index"]:
                relocations.append(r)
            else:
                droppedRel += 1
        for r in ph["sym"].get("unresolvedRelocations", []):
            if owner[r["imageOffsetHW"]] == ph["index"]:
                unresolved.append(r)
            else:
                droppedUnres += 1
    if droppedRel or droppedUnres:
        log.info(f"dropped {droppedRel} relocations / {droppedUnres} "
                 f"unresolved at sites overlaid by later phases")

    iplCutRepro = {}
    for ph in info:
        if ph.get("loadRangesHW") is not None:
            iplCutRepro["iplCut"] = {"phase": ph["phase"],
                                     "rangesHW": ph["loadRangesHW"]}
    if ops0:
        iplCutRepro["ops0Display"] = ops0

    first = info[0]["sym"]
    return {
        "version": first.get("version", ""),
        "imageSize": MEM_HW,
        "entryPoint": first.get("entryPoint"),
        "sections": sorted(sections, key=lambda s: (s["address"], s["name"])),
        "symbols": sorted(symbols, key=lambda s: (s["address"], s["name"])),
        "modules": modules,
        "relocations": sorted(relocations, key=lambda r: r["address"]),
        "unresolvedRelocations": sorted(unresolved,
                                        key=lambda r: r["imageOffsetHW"]),
        "repro": {
            "tool": "mmu2fcm",
            "config": cfgName,
            "phases": [ph["phase"] for ph in info if ph["phase"] is not None],
            **iplCutRepro,
            "constituents": {ph["name"]: ph["sym"].get("repro", {})
                             for ph in info if ph["phase"] is not None},
        },
    }


def stampIdentifiers(image, systemId=None, iloadId=None):
    """Write the IPL-set low-core identifiers into a composed image.

    The GPC's IPL loader writes the PASS System Identifier (hw 0x1C) and the
    I-Load Identifier (hw 0x20); the load module only RESERVES them (they
    read 0000).

    Returns the image (unchanged object when nothing was asked for)."""
    wanted = [(n, t, b, e) for n, t, b, e in (
        ("system", systemId, psaIdent.SYSTEM_ID_HW, psaIdent.parse_system_id),
        ("iload", iloadId, psaIdent.ILOAD_ID_HW, psaIdent.parse_iload_id))
        if t]
    if not wanted:
        return image
    buf = bytearray(image)
    for name, text, base, enc in wanted:
        raw = enc(text)                       # raises IdentError if malformed
        off = base * 2
        buf[off:off + psaIdent.IDENT_BYTES] = raw
        log.info(f"stamped {name} identifier {text} at hw {base:#06x} "
                 f"({raw.hex().upper()}) -- post-IPL memory, NOT load module")
    return bytes(buf)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="mmu2fcm",
        description="Compose linked phase .lib modules into a memory-"
                    "configuration .fcm image (GPC load emulation)")
    ap.add_argument("--mmu", default="build/mmu", type=Path,
                    help="con80build output root holding PHASEnn.lib "
                         "(default: build/mmu)")
    ap.add_argument("--config", default="SSW",
                    help="memory configuration name (known: %s; default SSW)"
                         % ", ".join(CONFIG_NAMES))
    ap.add_argument("--list-configs", action="store_true",
                    help="print the memory-configuration registry and exit")
    ap.add_argument("--phases",
                    help="comma-separated phase load order, overriding the "
                         "--config preset (e.g. 10,2,13,3)")
    ap.add_argument("--out", type=Path,
                    help="output directory (default: <mmu>/<config>)")
    ap.add_argument("--system-id", metavar="MM.NNP.JJA",
                    help="stamp the PASS System Identifier at hw 0x1C "
                         "(e.g. 29.01A.01B); default: leave it zero")
    ap.add_argument("--iload-id", metavar="XX.Y.Z.AS.BB",
                    help="stamp the I-Load Identifier at hw 0x20 "
                         "(e.g. 29.1.0.00.0B); default: leave it zero")
    # The ground Mass Memory Build stamps the #PFCMGPT/#PCDCPHA phase
    # tables into the phase-2 image on the tape (their load-module content
    # is initial zeros/-1).  Off by default: opt-in tape emulation
    # (ap101Utils.mmbstamp generates the values from the phase .libs and
    # the CON80 MMU decks).
    ap.add_argument("--stamp-phase-tables", action="store_true",
                    help="generate and stamp the Mass-Memory-Build phase "
                         "tables (#PFCMGPT, #PCDCPHA) into the composed "
                         "image; default: leave the load-module initials")
    ap.add_argument("--con80", type=Path, required=True,
                    help="CON80 deck directory (memory-configuration "
                         "registry and --stamp-phase-tables)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(name)s: %(message)s")

    try:
        deck = ConcardDeck(args.con80)
    except NotADirectoryError as e:
        ap.error(str(e))
    # an explicit --phases list needs only the deck (RESERVE cards, the
    # IPL-cut member), not the memory-configuration registry
    if args.list_configs or not args.phases:
        try:
            configs = load_configs(deck)
        except McConfigError as e:
            ap.error(str(e))
    if args.list_configs:
        print(listConfigs(configs))
        return 0

    if args.phases:
        phases = [int(p) for p in args.phases.split(",")]
    elif args.config in configs:
        phases = list(configs[args.config].phases)
    else:
        ap.error(f"unknown config {args.config!r} (known: "
                 f"{', '.join(configs)}) and no --phases given")

    # The IPL set stages its MFB (the last IPL phase) only up to the
    # GNC-decouple cut; every MC re-loads the full MFB at OPS transition,
    # so only the post-IPL SSW composition is affected.
    iplCut = None
    if args.config == "SSW":
        mfb = phases[-1]
        names, member = iplMfbRegions(deck, mfb)
        if names:
            log.info(f"IPL cut ({member}): PHASE{mfb:02d} stages only "
                     f"{'/'.join(names)}")
            iplCut = (mfb, names)

    out = args.out or (args.mmu / args.config)
    out.mkdir(parents=True, exist_ok=True)

    image, owner, info, ops0 = compose(args.mmu, phases, iplCut=iplCut,
                                       deck=deck)
    damaged = reportOverlays(image, owner, info)
    if damaged:
        log.warning(f"{damaged} cross-phase csect collisions (see above) -- "
                    f"composed bytes there are the later phase's text")
    sym = unionSym(args.config, owner, info, ops0)
    try:
        image = stampIdentifiers(image, args.system_id, args.iload_id)
    except psaIdent.IdentError as e:
        ap.error(str(e))
    if args.stamp_phase_tables:
        from ap101Utils import mmbstamp
        try:
            tables = mmbstamp.generate(args.mmu, args.con80)
            image = mmbstamp.stamp(image, sym, tables)
        except mmbstamp.StampError as e:
            ap.error(str(e))
        for n in tables.notes:
            log.debug(f"mmbstamp: {n}")
        log.info(f"stamped {mmbstamp.GPT_CSECT} ({mmbstamp.GPT_SIZE} hw) and "
                 f"{mmbstamp.CPHA_CSECT} ({mmbstamp.CPHA_SIZE} hw) -- "
                 f"Mass-Memory-Build tape content, NOT load module")

    fcm = out / f"{args.config}.fcm"
    fcm.write_bytes(image)
    symPath = out / f"{args.config}.sym.json"
    with open(symPath, "w") as f:
        json.dump(sym, f, indent=1)

    total = sum(ph["placedHW"] for ph in info)
    live = sum(1 for o in owner if o)
    log.info(f"{fcm}: {len(image)} bytes ({MEM_HW:#x} hw), {live} hw loaded "
             f"({total - live} hw overlaid), {len(sym['sections'])} sections, "
             f"{len(sym['relocations'])} relocations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
