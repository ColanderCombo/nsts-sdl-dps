#!/usr/bin/env python3
"""mmu2fcm -- emulate the GPC IPL/SSL load, composing a memory-configuration
image from the linked phase load modules (.lib).

The loader chain reads the MMU phase table and places each phase's load
blocks at their GPC main-memory addresses, in IPL order, later phases
overlaying earlier ones (FCOS overlay).  Our load blocks are the .lib TEXT
extents, already at absolute linked addresses, so the composition reads the
.lib files directly.

Memory not covered by any load block and alloc-only blocks keep the
IPL hardware background fill:
    C9FB in hw 0x00000-0x1FFFF,
    C6C6 in hw 0x20000-0x7FFFF (banks 0-3 vs 4-15).

Each checksum-group run (TEXT extent) is followed on tape by its 2-hw
fullword-aligned CHECKSUM slot, included in the load block.

Output is a phase-dir-shaped pair <out>/<NAME>.fcm + <NAME>.sym.json (the
union of the constituent phases' sym.json, overlay-aware).
"""
import argparse
import json
import logging
import sys
from pathlib import Path

from ap101Utils.concard import ConcardDeck, layout_program, phase_numbers
from ap101Utils.libModule import LibModule, regionIntervals
from ap101Utils.mcconfigs import (CONFIG_NAMES, IPL_PHASES, McConfigError,
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
SECTOR_HW = 0x8000                   # one BSR/DSR sector: a bootstrap copy
BOOT_PHASE = 1                       # PHASE01, the FMAIPL2 bootstrap copy


def bootImage(image: bytes, mmuRoot: Path, con80: Path) -> bytes:
    """The bootstrap copy over the IPL fill `compose` laid down (POO
    2.5.3.3), one sector.  Control reaches the loader through the system
    reset PSW at halfword 0x14 of the PSA the record carries."""
    from tools.mmu2mmv import bootRecord
    rec = bootRecord(mmuRoot, con80)
    if len(rec) != SECTOR_HW:
        raise ValueError(f"bootstrap record is {len(rec)} hw, not one "
                         f"{SECTOR_HW} hw sector")
    buf = bytearray(image)
    buf[0:2 * SECTOR_HW] = b"".join(w.to_bytes(2, "big") for w in rec)
    return bytes(buf)


def slotHalfwords(endHW: int) -> range:
    cap = ((endHW - 1) // BANK_HW + 1) * BANK_HW
    return range(endHW, min((endHW + 1) // 2 * 2 + 2, cap, MEM_HW))


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
    """The IPL image stages the MFB phase's load blocks only for the
    regions its structure member -- the deck member carrying the phase's
    BANK cards -- declares before the first BANK card.

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


def configMapRegions(deck: ConcardDeck, pgmPhases) -> list[str] | None:
    """PHASE02 regions a memory configuration keeps mapped: the union of
    the `MAP 2,<region...>` cards in the OPS-transition phases' layout
    programs.

    Returns None (no cut) when a phase member is missing or a bare
    `MAP 2` card maps the whole phase."""
    names: set[str] = set()
    for p in pgmPhases:
        member = f"PHASE{p:02d}"
        if not deck.has(member):
            log.warning(f"configMapRegions: no {member} deck member -- "
                        f"PHASE02 staged uncut")
            return None
        for op in layout_program(deck, member):
            if op.verb != "MAP" or not op.operand:
                continue
            parts = [t.strip() for t in op.operand.split(",")]
            if not parts[0].isdigit() or int(parts[0]) != 2:
                continue
            regs = [t for t in parts[1:] if t]
            if not regs:
                return None          # bare MAP 2: whole phase mapped
            names.update(regs)
    return sorted(names)


def chainReopenedRegions(deck: ConcardDeck, pgmPhases,
                         phase2Regions) -> set[str]:
    """PHASE02 region names re-OVERLAY'd (an origin-less OVERLAY card
    naming a known PHASE02 region) in the config chain's layout programs.

    `MAP 2,<region>` grants only the region's extent; when a chain phase
    re-opens the region with its own OVERLAY, the PHASE02 copies inside it
    are never loaded, so compose must cut PHASE02 there even though the
    region is mapped."""
    out: set[str] = set()
    for p in pgmPhases:
        member = f"PHASE{p:02d}"
        if not deck.has(member):
            continue
        for op in layout_program(deck, member):
            if op.verb != "OVERLAY" or not op.operand:
                continue
            name = op.operand.split(",")[0].strip()
            if getattr(op, "origin", None) is None and name in phase2Regions:
                out.add(name)
    return out


def reservedByPhase(deck: ConcardDeck) -> dict[int, list[str]]:
    """RESERVE-carded csects per phase segment.  The link allocates
    without text, but the MMB stages the allocation as a zeroed tape
    block."""
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


def compose(mmuRoot: Path, phases: list[int], iplCut=None, deck=None,
            regionCuts=None):
    """IPL fill + overlay each phase's TEXT extents in load order.
    Returns (image bytes, owner bytearray, per-phase info list).
    owner[hw] = 0 for background fill, else 1-based index into phases.

    iplCut = (phaseNum, [regionNames]): stage only that phase's extents
    intersecting the named regions' REGN intervals (the IPL image's
    GNC-decouple cut -- see iplMfbRegions).

    regionCuts = {phaseNum: [regionNames]}: same region-gated staging for
    other phases (the OPS configs' PHASE02 config-map cut -- see
    configMapRegions)."""
    cuts: dict[int, tuple[str, list[str], bool]] = {
        ph: ("config-map cut", names, True)
        for ph, names in (regionCuts or {}).items()}
    if iplCut:
        cuts[iplCut[0]] = ("IPL cut", iplCut[1], False)
    image = bytearray(FILL_LOW * FILL_SPLIT_HW +
                      FILL_HIGH * (MEM_HW - FILL_SPLIT_HW))
    owner = bytearray(MEM_HW)
    info = []
    for idx, n in enumerate(phases, start=1):
        name, lib, sym = loadPhase(mmuRoot, n)
        rangesHW = None
        if n in cuts:
            label, regionNames, extend = cuts[n]
            # The config-map cut extends each mapped interval to the next
            # REGN record (linker-grown pool space belongs to its region);
            # the IPL cut keeps the records exactly, its regions being
            # OVERLAY-declared.  See libModule.regionIntervals.
            ivals = regionIntervals(lib, regionNames, extend=extend)
            missing = set(regionNames) - {r.name for r in lib.regions}
            if missing:
                log.warning(f"{name}: no REGN record for staged "
                            f"region(s) {sorted(missing)}")
            kept = [x for x in lib.extents
                    if any(x.address < e and x.address + len(x.data) > s
                           for s, e in ivals)]
            cut = len(lib.extents) - len(kept)
            cutHW = sum(len(x.data) for x in lib.extents
                        if x not in kept) // 2
            lib.extents = kept
            rangesHW = [[s // 2, e // 2] for s, e in sorted(ivals)]
            tail = ("deferred to OPS transition" if label == "IPL cut"
                    else "OPS0-only, not loaded by this MC")
            log.info(f"{name}: {label} -- staging only "
                     f"{len(ivals)}/{len(regionNames)} mapped regions; "
                     f"{cut} extents ({cutHW} hw) {tail}")
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
    # real text wins.
    if deck is not None and info:
        bootRes = info[0]["index"]
        resv = reservedByPhase(deck)
        tailHW = set()
        for ph in info:
            if ph["index"] == bootRes or ph["lib"] is None:
                continue
            for x in ph["lib"].extents:
                tailHW.update(
                    slotHalfwords((x.address + len(x.data)) // 2))
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
                    if i in tailHW:
                        continue      # another block's checksum slot/pad
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
    # TEXT claimed the bytes.
    bootIdx = info[0]["index"] if info else 0
    slot_hw = set()
    for ph in info:
        if ph["index"] == bootIdx:
            continue          # boot-block tails live in unlisted holes
        ends = ([(x.address + len(x.data)) // 2 for x in ph["lib"].extents]
                if ph["lib"] is not None else [])
        ends += [a + n for a, n in ph.get("extentsHW", [])]
        for end in ends:
            for i in slotHalfwords(end):
                if owner[i] in (0, bootIdx):
                    image[2 * i:2 * i + 2] = SLOT_FILL
                    slot_hw.add(i)
    if slot_hw:
        log.info(f"checksum slots: {len(slot_hw)} background hw filled C6C6")
    return bytes(image), owner, info, ops0


def ops0DisplayLoad(mmuRoot: Path, image: bytearray, owner: bytearray,
                    info: list[dict], bootPhase: int) -> dict | None:
    """Stage the SSW/OPS0 display roll-in."""
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
            for i in slotHalfwords((x.address + len(x.data)) // 2):
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


def ownerRuns(owner: bytearray, info: list[dict]) -> list[list[int]]:
    """Run-length encode compose()'s per-halfword load ownership.

    Returns [[startHW, phase], ...] ascending, each run reaching to the next
    run's start; `phase` is the phase number whose text the composed image
    holds there, 0 for background/staging fill no phase placed, and -1 for a
    compose-time pseudo phase (the OPS0 display roll-in).

    The patch chain needs this: a UPF deck patches one phase's tape load
    block, and a later phase may overlay part of it, so the patched content
    must not appear where a later phase owns the halfword
    (tools/iload/upfapply.py)."""
    byIndex = {ph["index"]: (ph["phase"] if ph["phase"] is not None else -1)
               for ph in info}
    runs: list[list[int]] = []
    prev = None
    for h, o in enumerate(owner):
        v = byIndex.get(o, 0) if o else 0
        if v != prev:
            runs.append([h, v])
            prev = v
    return runs


def unionSym(cfgName: str, owner: bytearray, info: list[dict],
             ops0: dict | None = None,
             mcfPhases: 'frozenset[int] | None' = None) -> dict:
    """Merge the constituent phases' sym.json overlay-aware:
    - sections/symbols: entries whose module carries text (no '>' marker)
      beat imported markers; among text-bearing duplicates the later-loaded
      phase wins (overlay semantics).
    - relocations / unresolvedRelocations: kept only if the fixup site still
      belongs to that phase in the composed image (not overlaid later).
    - storeProtect: each phase's map masked to the halfwords it still owns;
      absent when no phase carries one.
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
        phase's text fully overwrote.  Text-less sections (stacks,
        RESERVEs) always survive."""
        if imported(e) or ph["lib"] is None:
            return False
        a, n = e["address"], max(e.get("size", 1), 1)
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
                     f"phases dropped (only the winner is listed)")
        return [e for _, e in out.values()]

    sections = mergeNamed("sections")
    symbols = mergeNamed("symbols")

    if mcfPhases is not None:
        def _marker(e):
            m = e.get("module", "")
            return m.endswith(">") and not m.startswith("<")
        listed = {e["name"] for ph in info
                  if ph["phase"] is None or ph["phase"] in mcfPhases
                  for e in ph["sym"].get("sections", [])
                  if staged(ph, e) and not _marker(e)}
        marked = 0
        for s in sections:
            if s["name"] not in listed:
                s["residue"] = True
                marked += 1
        if marked:
            log.info(f"{marked} section(s) marked MCF residue "
                     f"(phases outside {sorted(mcfPhases)})")

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

    # Store protection, overlay-aware: each constituent's linker map masked
    # to the halfwords it still owns in the composed image.  Omitted when no
    # constituent carries one; an empty map would claim the image is
    # unprotected.
    protect = bytearray(MEM_HW)
    mapped, bare = 0, []
    for ph in info:
        sp = ph["sym"].get("storeProtect")
        if not sp or sp.get("unit") != "halfword":
            bare.append(ph["name"])
            continue
        mapped += 1
        idx = ph["index"]
        for lo, hi in sp.get("ranges", []):
            for a in range(max(lo, 0), min(hi, MEM_HW)):
                if owner[a] == idx:
                    protect[a] = 1
    protRanges, a = [], 0
    while a < MEM_HW:
        if not protect[a]:
            a += 1
            continue
        b = a
        while b < MEM_HW and protect[b]:
            b += 1
        protRanges.append([a, b])
        a = b
    if mapped:
        log.info(f"store protect: {sum(b - a for a, b in protRanges)} hw in "
                 f"{len(protRanges)} ranges")
        if bare:
            log.info(f"store protect: no map from {'/'.join(bare)} -- "
                     f"those halfwords compose unprotected")
    else:
        log.warning("no constituent carries a store-protect map; the image "
                    "gets none (relink the phases to regenerate)")

    iplCutRepro = {}
    cutList = [{"phase": ph["phase"], "rangesHW": ph["loadRangesHW"]}
               for ph in info if ph.get("loadRangesHW") is not None]
    if cutList:
        # legacy single-cut key (kept for older readers) + the full list
        iplCutRepro["iplCut"] = cutList[-1]
        iplCutRepro["regionCuts"] = cutList
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
        **({"storeProtect": {"unit": "halfword", "ranges": protRanges}}
           if mapped else {}),
        "relocations": sorted(relocations, key=lambda r: r["address"]),
        "unresolvedRelocations": sorted(unresolved,
                                        key=lambda r: r["imageOffsetHW"]),
        "repro": {
            "tool": "mmu2fcm",
            "config": cfgName,
            "phases": [ph["phase"] for ph in info if ph["phase"] is not None],
            **({"mcfPhases": sorted(mcfPhases)} if mcfPhases else {}),
            **iplCutRepro,
            "constituents": {ph["name"]: ph["sym"].get("repro", {})
                             for ph in info if ph["phase"] is not None},
        },
    }


def stampIdentifiers(image, systemId=None, iloadId=None):
    """Write the IPL-set low-core identifiers into a composed image.

    The GPC's IPL loader writes the PASS System Identifier (hw 0x1C) and
    the I-Load Identifier (hw 0x20); the image comes back unchanged when
    neither was asked for."""
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
                 f"({raw.hex().upper()}) -- post-IPL memory")
    return bytes(buf)


def stampChecksums(image, owner, info, mmuRoot: Path, src, skip: set,
                   ops0: dict | None = None
                   ) -> tuple[bytes, list[int], list[list[int]]]:
    """--stamp-checksums: write each tape load block's trailing checksum.

    The MMB computes it over the block as written to tape -- the phase's
    own text with the INIT=C6C6 staging fill in every pad/gap halfword --
    and stores (0000, sum16) in the block's closing 2-hw slot.

    Values are summed from the uncut .lib on disk (the tape carries the
    whole phase; a config's region cut only decides what the loader
    places), over [start, slot): the slot itself is excluded.  A slot is
    written only where no non-boot phase supplied text (same guard as the
    C6C6 slot pass; boot residue is dead memory a staged slot overwrites).

    A tail whose slot immediately precedes an OPS0 display roll-in
    placement is left C6C6: the DMM stages those blocks post-IPL with
    content only, and the tape-record boundary the roll-in displaced is an
    earlier one.

    Returns (image, sorted slot addresses stamped, [[hw, phase], ...]).
    The addresses go to the composed manifest so the listing can star
    them: memory differs from the load module there.
    """
    from ap101Utils import mmbstamp
    buf = bytearray(image)
    bootIdx = info[0]["index"] if info else 0
    rollin = {a for a, _ in (ops0 or {}).get("extentsHW", [])}
    stamped: dict[int, int] = {}
    lbSlot: dict[int, int] = {}      # slot hw -> assigned phase closing it
    for ph in info:
        n = ph["phase"]
        if n is None or n in skip or (n != 2 and n not in src.assigned):
            continue                  # OPS0DSP pseudo-phase / boot / IPL-cut
        # phase_load_blocks is the one definition of a phase's partition:
        # it also runs fit_budget, which backs a bank tail up to keep the
        # phase inside its ALLOC BLKS.  It reads the .lib uncut, off disk,
        # because the MMB sums a block over the tape record.
        lbs = mmbstamp.phase_load_blocks(Path(mmuRoot), n, src)[0]
        # The tape record, not the .lib: an auto-generated @-stack is
        # allocation with no tape text, so the MMB's staging fill covers
        # it (mmbstamp.stack_holes).
        text = mmbstamp.tape_text(Path(mmuRoot), n)
        wrote = 0
        for lb in lbs:
            slot = lb.start + lb.length - 2
            # The test fcmImage._checksum_slots uses, `owner[a] < order`:
            # a configuration loads phase by phase and a later phase's
            # block tail overwrites an earlier phase's text, so only text
            # at or after this block's load order takes the slot away.
            if (slot + 2 > MEM_HW
                    or owner[slot] >= ph["index"]
                    or owner[slot + 1] >= ph["index"]):
                continue              # a later phase's text owns the slot
            if n in src.assigned:
                lbSlot[slot] = lbSlot[slot + 1] = n   # later phase wins
            if slot + 2 in rollin:
                continue              # OPS0 roll-in boundary: stays C6C6
            s = 0
            for h in range(lb.start, slot):
                s += text.get(h, mmbstamp.FILL)
            stamped[slot] = s & 0xFFFF    # load order: later phase wins
            wrote += 1
        log.info(f"PHASE{n:02d}: {wrote}/{len(lbs)} load-block checksums "
                 f"stamped")
    for slot, v in stamped.items():
        buf[2 * slot:2 * slot + 4] = bytes(2) + v.to_bytes(2, "big")
    if stamped:
        log.info(f"stamped {len(stamped)} load-block checksum slots -- "
                 f"Mass-Memory-Build content")
    return (bytes(buf), sorted(stamped),
            [[a, lbSlot[a]] for a in sorted(lbSlot)])


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
                         "(e.g. 29.01A.01B)")
    ap.add_argument("--iload-id", metavar="XX.Y.Z.AS.BB",
                    help="stamp the I-Load Identifier at hw 0x20 "
                         "(e.g. 29.1.0.00.0B)")
    ap.add_argument("--stamp-phase-tables",
                    action=argparse.BooleanOptionalAction, default=False,
                    help="generate and stamp the Mass-Memory-Build tape "
                         "tables (#PFCMGPT, #PCDCPHA, FCMG3DAT)")
    ap.add_argument("--stamp-g3dat", action="store_true",
                    help="stamp just the G3 archive destination address "
                         "table (FCMG3DAT)")
    ap.add_argument("--stamp-checksums",
                    action=argparse.BooleanOptionalAction, default=False,
                    help="compute and stamp each tape load block's checksum")
    ap.add_argument("--stamp-ipl-tables",
                    action=argparse.BooleanOptionalAction, default=False,
                    help="generate and stamp the IPL set's tables "
                         "(FCMSSLPT, FCMCKSUM)")
    ap.add_argument("--boot", action="store_true",
                    help="compose the machine as the IPL microcode leaves "
                         "it; implies --phases 1 and names the output "
                         "BOOT")
    ap.add_argument("--con80", type=Path, required=True,
                    help="CON80 deck directory")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(name)s: %(message)s")

    try:
        deck = ConcardDeck(args.con80)
    except NotADirectoryError as e:
        ap.error(str(e))
    if args.list_configs or not args.phases:
        try:
            configs = load_configs(deck)
        except McConfigError as e:
            ap.error(str(e))
    if args.list_configs:
        print(listConfigs(configs))
        return 0

    if args.boot and not args.phases:
        args.phases = str(BOOT_PHASE)
        if args.config == "SSW":              # the parser default
            args.config = "BOOT"
    if args.phases:
        phases = [int(p) for p in args.phases.split(",")]
    elif args.config in configs:
        phases = list(configs[args.config].phases)
    else:
        ap.error(f"unknown config {args.config!r} (known: "
                 f"{', '.join(configs)}) and no --phases given")

    iplCut = None
    if args.config == "SSW":
        mfb = phases[-1]
        names, member = iplMfbRegions(deck, mfb)
        if names:
            log.info(f"IPL cut ({member}): PHASE{mfb:02d} stages only "
                     f"{'/'.join(names)}")
            iplCut = (mfb, names)

    regionCuts = None
    if not args.phases and args.config != "SSW" and args.config in configs:
        pgm = [p for p in phases if p not in IPL_PHASES]
        names = configMapRegions(deck, pgm)
        if names and 2 in phases:
            p2lib = args.mmu / "PHASE02.lib"
            if p2lib.exists():
                p2names = {r.name
                           for r in LibModule.read(p2lib).regions}
                reopened = chainReopenedRegions(deck, pgm, p2names)
                dropped = sorted(set(names) & reopened)
                if dropped:
                    log.info(f"config-map cut ({args.config}): "
                             f"{len(dropped)} mapped region(s) re-opened "
                             f"by the chain, PHASE02 copies not loaded: "
                             f"{'/'.join(dropped)}")
                    names = sorted(set(names) - reopened)
            log.info(f"config-map cut ({args.config}): PHASE02 stages "
                     f"only {len(names)} mapped regions")
            regionCuts = {2: names}

    out = args.out or (args.mmu / args.config)
    out.mkdir(parents=True, exist_ok=True)

    image, owner, info, ops0 = compose(args.mmu, phases, iplCut=iplCut,
                                       deck=deck, regionCuts=regionCuts)
    damaged = reportOverlays(image, owner, info)
    if damaged:
        log.warning(f"{damaged} cross-phase csect collisions (see above) -- "
                    f"composed bytes there are the later phase's text")
    mcf = None
    if not args.phases and args.config in configs:
        mcf = configs[args.config].mcf_phases
    sym = unionSym(args.config, owner, info, ops0, mcfPhases=mcf)
    sym["ownerPhaseRunsHW"] = ownerRuns(owner, info)
    if args.boot:
        from ap101Utils import mmbstamp
        try:
            image = bootImage(image, args.mmu, args.con80)
        except (mmbstamp.StampError, ValueError) as e:
            ap.error(str(e))
        sym["storeProtect"] = {"unit": "halfword", "ranges": [[0, MEM_HW]]}
        log.info(f"bootstrap copy over sector zero ({SECTOR_HW} hw), "
                 f"{mmbstamp.BOOT_PT_SYMS[0]} stamped, "
                 f"{MEM_HW} hw store protected")
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
        from ap101Utils import g3dat as _g3
        _extra = (f" and {_g3.G3DAT_CSECT} ({_g3.G3DAT_SIZE} hw)"
                  if tables.g3dat else
                  f" (no {_g3.G3DAT_CSECT}: see notes)")
        log.info(f"stamped {mmbstamp.GPT_CSECT} ({mmbstamp.GPT_SIZE} hw), "
                 f"{mmbstamp.CPHA_CSECT} ({mmbstamp.CPHA_SIZE} hw)"
                 f"{_extra} -- Mass-Memory-Build content")
    if args.stamp_g3dat:
        from ap101Utils import g3dat as g3datmod
        from ap101Utils import mmbstamp
        try:
            dat = g3datmod.generate(args.mmu, args.con80)
            image = g3datmod.stamp(image, sym, dat)
        except mmbstamp.StampError as e:      # incl. g3dat.G3DatError
            ap.error(str(e))
        for n in dat.notes:
            log.debug(f"g3dat: {n}")
        log.info(f"stamped {g3datmod.G3DAT_CSECT} ({g3datmod.G3DAT_SIZE} hw, "
                 f"{len(dat.entries)} entries) -- Mass-Memory-Build content")
    if args.stamp_ipl_tables:
        from ap101Utils import mmbstamp
        try:
            sslpt, notes = mmbstamp.generate_sslpt(args.mmu, args.con80)
            image = mmbstamp.stamp_ipl(image, sym, sslpt)
        except mmbstamp.StampError as e:
            ap.error(str(e))
        for n in notes:
            log.debug(f"sslpt: {n}")
        start, count = mmbstamp.ssl_extent(sym)
        log.info(f"stamped {mmbstamp.SSLPT_CSECT} ({mmbstamp.SSLPT_SIZE} hw) "
                 f"and {mmbstamp.CKSUM_CSECT} over {count} hw at "
                 f"{start:#07x} -- Mass-Memory-Build content")
    if args.stamp_checksums:
        from ap101Utils import mmbstamp
        try:
            csrc = mmbstamp.load_phase_source(args.con80)
        except mmbstamp.StampError as e:
            ap.error(str(e))
        skip = {phases[0]}                    # boot-block tails: unlisted
        if iplCut:
            skip.add(iplCut[0])               # IPL load deposits no tails
        image, slots, lbSlots = stampChecksums(image, owner, info, args.mmu,
                                               csrc, skip, ops0=ops0)
        # the listing stars these rows: memory differs from the load module
        sym["stampedChecksums"] = slots
        sym["lbSlotPhases"] = lbSlots

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
