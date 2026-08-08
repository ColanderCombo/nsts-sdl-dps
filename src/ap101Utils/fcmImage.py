"""Reader for a composed .fcm memory image (mmu2fcm output).

Annotates any halfword address with its csect, offset, phase, module and
nearest symbol.  Holds no comparison or diff-suppression policy.

Metadata comes from two places:
  1. ``<config>.sym.json`` beside the image -- the composed manifest
     (sections/symbols/relocations, plus a repro block naming the config
     and the phase load order).
  2. The con80build output root (default build/mmu): each
     PHASEnn/PHASEnn.sym.json supplies per-csect phase attribution, and
     each PHASEnn.lib supplies the TEXT extents that give the occupancy
     map.  Later phases overlay earlier ones.

``words`` covers every halfword in the image, including the fill mmu2fcm
renders where no load block reaches.  ``occupied`` is the subset actually
backed by a .lib TEXT extent; the rest is IPL background, or RESERVE space
that reads as fill.  Without the .lib files occupancy degrades to the union
of csect ranges -- a superset, since RESERVE regions are included -- and a
warning is recorded.
"""
from __future__ import annotations

import json
import struct
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path

from .libModule import LibModule

BANK_HW = 0x8000              # a checksum slot never crosses a bank boundary


@dataclass(eq=False)          # identity semantics: csects go in sets below
class FcmCsect:
    name: str
    start: int          # hw address in the composed image
    size: int           # hw count
    phase: str          # winning (composing) phase, e.g. 'PHASE08'
    module: str | None = None
    imported: bool = False      # address-only marker, no text supplied
    reserved: bool = False      # RESERVE-carded: zeroed MMB block, no text

    @property
    def end(self) -> int:       # exclusive
        return self.start + self.size


@dataclass
class FcmLocation:
    csect: str | None      # None = no csect claims the address (fill)
    offset: int            # hw offset into the csect (0 when csect is None)
    phase: str | None
    module: str | None
    symbol: str | None     # nearest symbol at or below, within the csect
    symbol_offset: int     # hw offset from that symbol


def _imported(entry: dict) -> bool:
    """True for an address-only section imported from an earlier phase.

    'NAME>' marks the import; '<generated-...>' names the linker's own
    synthesized csects (Z1-pool ZCON stubs, @-stacks), which do carry text.
    """
    m = entry.get("module", "")
    return m.endswith(">") and not m.startswith("<")


class FcmImage:
    """A composed .fcm image plus its metadata; see module docstring."""

    def __init__(self):
        self.config: str = ""
        self.words: dict[int, int] = {}
        self.csects: list[FcmCsect] = []
        self.shadowed: list[FcmCsect] = []   # same-name overlay losers
        self.occupied: set[int] = set()
        self.checksum_slots: list[int] = []  # fullword-aligned 2-hw slots
        self.warnings: list[str] = []
        self.mem_hw: int = 0
        self.phases: list[str] = []          # load order
        self.manifest: dict = {}             # composed <config>.sym.json
        # lookup structures
        self._segs: list[tuple[int, int, FcmCsect]] = []
        self._seg_starts: list[int] = []
        self._sym_by_sect: dict[str, tuple[list[int], list[str]]] = {}
        self._ipl_cut = None                 # (phase, [(startHW, endHW)])
        self._ops0: dict | None = None       # compose-time display staging

    #
    # loading
    #
    @classmethod
    def load(cls, fcm_path, mmu_dir=None) -> "FcmImage":
        self = cls()
        fcm_path = Path(fcm_path)
        raw = fcm_path.read_bytes()
        if len(raw) % 2:
            self.warnings.append(f"{fcm_path}: odd byte length {len(raw)}")
            raw = raw[:-1]
        self.mem_hw = len(raw) // 2
        vals = struct.unpack(f">{self.mem_hw}H", raw)
        self.words = dict(enumerate(vals))

        manifest = self._read_manifest(fcm_path)
        self.config = (manifest.get("repro", {}).get("config")
                       or fcm_path.stem)
        mmu_root = self._find_mmu_root(fcm_path, mmu_dir)

        repro = manifest.get("repro", {})
        phase_nums = repro.get("phases", [])
        self.phases = [f"PHASE{n:02d}" for n in phase_nums]
        # IPL GNC-decouple cut: the named phase was staged only inside
        # these ranges; its sections and extents elsewhere load later, at
        # the OPS transition, so they are not in this image
        cut = repro.get("iplCut")
        self._ipl_cut = ((f"PHASE{cut['phase']:02d}",
                          [tuple(r) for r in cut["rangesHW"]])
                         if cut else None)
        # OPS0 display roll-in: sections synthesized at compose time, so
        # they appear only in the composed manifest
        self._ops0 = repro.get("ops0Display")
        phase_syms = self._load_phase_syms(mmu_root)

        self._build_csects(phase_syms)
        self._build_occupancy(mmu_root, phase_nums)
        self._build_segments()
        self._index_symbols()
        self._validate()
        return self

    def _read_manifest(self, fcm_path: Path) -> dict:
        sym_path = fcm_path.with_name(fcm_path.stem + ".sym.json")
        if sym_path.exists():
            self.manifest = json.load(open(sym_path))
        else:
            self.warnings.append(
                f"composed manifest {sym_path} not found -- no csect or "
                f"symbol metadata available")
            self.manifest = {}
        return self.manifest

    def _find_mmu_root(self, fcm_path: Path, mmu_dir) -> Path | None:
        candidates = []
        if mmu_dir is not None:
            candidates.append(Path(mmu_dir))
        # mmu2fcm's default output layout is <mmu>/<config>/<config>.fcm
        candidates.append(fcm_path.parent.parent)
        candidates.append(Path("build/mmu"))
        for c in candidates:
            if c and any(c.glob("PHASE??.lib")):
                return c
        self.warnings.append(
            "no con80build output root with PHASEnn.lib found "
            f"(tried {', '.join(str(c) for c in candidates)}) -- "
            "occupancy falls back to csect ranges (RESERVE included)")
        return None

    def _load_phase_syms(self, mmu_root: Path | None) -> list[tuple[str, dict]]:
        """[(phase, symJson)] in load order; missing files warn and skip."""
        out = []
        if mmu_root is None:
            return out
        for name in self.phases:
            p = mmu_root / name / f"{name}.sym.json"
            if not p.exists():
                self.warnings.append(f"{p} missing -- csects from {name} "
                                     f"cannot be phase-attributed")
                continue
            out.append((name, json.load(open(p))))
        return out

    #
    # csect merge
    #
    def _build_csects(self, phase_syms: list):
        """Merge the per-phase sections, recording which phase won each.

        Text-bearing entries beat imported markers; among text-bearing
        duplicates the later-loaded phase wins, and the losers are kept in
        ``shadowed``.  This mirrors the merge mmu2fcm.unionSym performs.
        """
        manifest = self.manifest
        if phase_syms:
            # The composed manifest is the config map; the per-phase syms
            # only refine attribution.  A name the manifest omits was
            # dropped at compose time (IPL cut, or fully overlaid).
            mnames = ({s["name"] for s in manifest.get("sections", [])}
                      if manifest.get("sections") else None)
            best: dict[str, tuple[dict, str]] = {}
            for pname, sym in phase_syms:            # load order
                for e in sym.get("sections", []):
                    if mnames is not None and e["name"] not in mnames:
                        continue
                    if not self._staged(pname, e):
                        continue
                    cur = best.get(e["name"])
                    if cur is None or _imported(cur[0]) or not _imported(e):
                        if (cur is not None and not _imported(cur[0])
                                and not _imported(e)):
                            self.shadowed.append(self._csect(cur[0], cur[1]))
                        best[e["name"]] = (e, pname)
            # Display roll-in sections exist only in the composed
            # manifest, so no phase claims them.
            if self._ops0:
                by_name = {e["name"]: e for e in manifest.get("sections", [])}
                for n in self._ops0.get("sections", {}):
                    e = by_name.get(n)
                    if e is not None and n not in best:
                        best[n] = (e, "OPS0DSP")
            self.csects = [self._csect(e, pname) for e, pname in best.values()]
        elif manifest.get("sections"):
            self.warnings.append("per-phase sym.json unavailable -- csect "
                                 "phase attribution degraded to config name")
            self.csects = [self._csect(e, self.config)
                           for e in manifest["sections"]]
        # RESERVE is a compose-time flag, so it appears only on the
        # composed manifest's entries.
        mres = {s["name"] for s in manifest.get("sections", [])
                if s.get("reserved")}
        for c in self.csects:
            if c.name in mres:
                c.reserved = True
        self.csects.sort(key=lambda c: (c.start, c.name))
        self.shadowed.sort(key=lambda c: (c.start, c.name))

    def _staged(self, pname: str, e: dict) -> bool:
        """False for an IPL-cut phase's own section lying wholly outside
        its staged ranges.  Imported markers describe another phase's
        space, so they are always staged."""
        if self._ipl_cut is None or pname != self._ipl_cut[0] \
                or _imported(e):
            return True
        a, n = e["address"], max(e.get("size", 1), 1)
        return any(a < end and a + n > start
                   for start, end in self._ipl_cut[1])

    def _csect(self, e: dict, phase: str) -> FcmCsect:
        return FcmCsect(name=e["name"], start=e["address"], size=e["size"],
                        phase=phase, module=e.get("module"),
                        imported=_imported(e))

    #
    # occupancy
    #
    def _build_occupancy(self, mmu_root: Path | None, phase_nums: list[int]):
        """Mark the halfwords a phase .lib TEXT extent covers, laid down the
        way mmu2fcm.compose lays them.  Also derives the checksum slots.
        Without the .lib files, falls back to the union of csect ranges.
        """
        marked = bytearray(self.mem_hw)
        # The boot phase is excluded from slot suppression: its residue is
        # dead memory that later tape blocks stamp their slots over, and
        # its own block tails sit in unlisted holes.
        nonboot = bytearray(self.mem_hw)
        boot = f"PHASE{phase_nums[0]:02d}" if phase_nums else None
        extent_ends: list[int] = []
        derived = False

        def claim(hw0: int, nhw: int, slot: bool = True):
            marked[hw0:hw0 + nhw] = b"\x01" * nhw
            nonboot[hw0:hw0 + nhw] = b"\x01" * nhw
            if slot:
                extent_ends.append(hw0 + nhw)

        if mmu_root is not None and phase_nums:
            for n in phase_nums:
                name = f"PHASE{n:02d}"
                libp = mmu_root / f"{name}.lib"
                if not libp.exists():
                    self.warnings.append(f"{libp} missing -- occupancy "
                                         f"incomplete for {name}")
                    continue
                for x in LibModule.read(libp).extents:
                    if self._behind_ipl_cut(name, x):
                        continue
                    if (x.address % 2
                            or x.address + len(x.data) > 2 * self.mem_hw):
                        self.warnings.append(
                            f"{name}: extent 0x{x.address:06X} len "
                            f"{len(x.data)} outside image or unaligned")
                        continue
                    hw0, nhw = x.address // 2, len(x.data) // 2
                    if name == boot:
                        marked[hw0:hw0 + nhw] = b"\x01" * nhw
                    else:
                        claim(hw0, nhw)
                derived = True

        if derived:
            # OPS0 display roll-in extents are staged at compose time but
            # are ordinary tape blocks, trailing checksum slot included.
            for a, n in (self._ops0 or {}).get("extentsHW", []):
                claim(a, n)
            # A RESERVE-carded csect has no .lib text: the MMB stages the
            # allocation as a zeroed tape block, so it occupies memory and
            # earns a trailing slot like any other block.
            for s in self.manifest.get("sections", []):
                if s.get("reserved"):
                    claim(s["address"], s["size"])
            self.checksum_slots = self._checksum_slots(extent_ends, nonboot)
        else:
            for c in self.csects:
                if c.imported:
                    continue
                a, b = max(c.start, 0), min(c.end, self.mem_hw)
                marked[a:b] = b"\x01" * max(b - a, 0)

        self.occupied = {i for i, m in enumerate(marked) if m}

    def _behind_ipl_cut(self, phase: str, extent) -> bool:
        """True when this phase was IPL-cut and the extent lies wholly
        outside the staged ranges, so the image never received it."""
        if self._ipl_cut is None or phase != self._ipl_cut[0]:
            return False
        lo, hi = extent.address, extent.address + len(extent.data)
        return not any(lo < 2 * end and hi > 2 * start
                       for start, end in self._ipl_cut[1])

    def _checksum_slots(self, extent_ends, nonboot) -> list[int]:
        """Each tape block is followed by a fullword-aligned 2-hw checksum
        slot that never crosses a bank boundary.  A slot survives in memory
        where no later block's text claims it, and shows up in the listing
        as a '--------' row."""
        slots = set()
        for end in extent_ends:
            start = (end + 1) // 2 * 2
            cap = ((end - 1) // BANK_HW + 1) * BANK_HW
            if start + 2 <= min(cap, self.mem_hw) \
                    and not nonboot[start] and not nonboot[start + 1]:
                slots.add(start)
        return sorted(slots)

    #
    # address lookup
    #
    def _build_segments(self):
        """Sweep the csects into disjoint segments so locate() is one
        bisect.  Overlaps resolve by overlay order: the latest-loaded phase
        wins, then the smallest (most specific) csect."""
        order = {name: i for i, name in enumerate(self.phases, start=1)}
        events: dict[int, list[tuple[bool, FcmCsect]]] = {}
        for c in self.csects:
            a, b = max(c.start, 0), min(c.end, self.mem_hw)
            if c.size <= 0 or a >= b:
                continue
            events.setdefault(a, []).append((True, c))
            events.setdefault(b, []).append((False, c))
        active: set[FcmCsect] = set()
        warned_pairs: set[tuple[str, str]] = set()
        self._segs = []
        prev = None
        for pos in sorted(events):
            if prev is not None and active and pos > prev:
                self._segs.append(
                    (prev, pos, self._winner(active, order, warned_pairs)))
            for starting, c in events[pos]:
                (active.add if starting else active.discard)(c)
            prev = pos
        self._seg_starts = [s for s, _, _ in self._segs]

    def _winner(self, active, order, warned_pairs) -> FcmCsect:
        """The csect that owns a segment; same-phase overlap is outside
        known overlay behavior, so it warns once per pair."""
        if len(active) == 1:
            return next(iter(active))
        cs = sorted(active, key=lambda c: (-order.get(c.phase, 0),
                                           c.size, c.name))
        win = cs[0]
        for other in cs[1:]:
            if order.get(other.phase, 0) != order.get(win.phase, 0):
                continue
            pair = tuple(sorted((win.name, other.name)))
            if pair not in warned_pairs:
                warned_pairs.add(pair)
                self.warnings.append(
                    f"overlap beyond overlay behavior: {pair[0]} and "
                    f"{pair[1]} (both {win.phase}) share address space")
        return win

    def _index_symbols(self):
        """Sorted per-csect symbol lists for nearest-at-or-below lookup.
        A section symbol anchors its own csect at offset 0."""
        by: dict[str, list[tuple[int, str]]] = {}
        for s in self.manifest.get("symbols", []):
            sect = s.get("section")
            if sect is None and s.get("kind") == "section":
                sect = s["name"]
            if sect is None:
                continue
            by.setdefault(sect, []).append((s["address"], s["name"]))
        for sect, lst in by.items():
            lst.sort()
            self._sym_by_sect[sect] = ([a for a, _ in lst],
                                       [n for _, n in lst])

    def locate(self, hw: int) -> FcmLocation:
        """Annotate halfword address ``hw``; valid for any address in
        ``words``.  A csect of None means no csect claims it (fill)."""
        c = None
        i = bisect_right(self._seg_starts, hw) - 1
        if i >= 0:
            a, b, cand = self._segs[i]
            if a <= hw < b:
                c = cand
        if c is None:
            return FcmLocation(csect=None, offset=0, phase=None, module=None,
                               symbol=None, symbol_offset=0)
        symbol, sym_off = None, 0
        addrs_names = self._sym_by_sect.get(c.name)
        if addrs_names:
            addrs, names = addrs_names
            j = bisect_right(addrs, hw) - 1
            if j >= 0 and addrs[j] >= c.start:
                symbol, sym_off = names[j], hw - addrs[j]
        return FcmLocation(csect=c.name, offset=hw - c.start, phase=c.phase,
                           module=c.module, symbol=symbol,
                           symbol_offset=sym_off)

    #
    # validation
    #
    def _validate(self):
        for c in self.csects:
            if c.start < 0 or c.end > self.mem_hw:
                self.warnings.append(
                    f"csect {c.name} ({c.phase}) claims hw "
                    f"{c.start:#07x}..{c.end:#07x} outside image "
                    f"(0..{self.mem_hw:#07x})")
        # the merged csect set should agree with the composed manifest
        man = {(e["name"], e["address"], e["size"])
               for e in self.manifest.get("sections", [])}
        if man:
            ours = {(c.name, c.start, c.size) for c in self.csects}
            for name, addr, size in sorted(man - ours):
                self.warnings.append(
                    f"manifest section {name} @hw {addr:#07x} size {size} "
                    f"not reproduced by per-phase merge")
            for name, addr, size in sorted(ours - man):
                self.warnings.append(
                    f"merged csect {name} @hw {addr:#07x} size {size} "
                    f"absent from composed manifest")
