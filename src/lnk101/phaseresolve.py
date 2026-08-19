#!/usr/bin/env python3
#
# Cross-phase resolution: fix up each phase load module's residual RLDs once
# every phase's addresses are known.
#
# The flight linkedit was one link per memory-configuration composition, so
# a cross-phase reference got a real address baked in only when a phase in
# the SAME composition defined it; references to phases outside the
# composition stay zero, and runtime correctness comes from OPS/config
# gating.  Our per-phase links leave those sites as addend/0 with the RLDs
# retained in the .lib; this pass replays them against the union of the
# phases sharing at least one memory configuration with the consuming phase
# (membership inverted from the ap101Utils.mcconfigs registry), and patches
# both the .lib extents and the .fcm image.  Phases in no configuration
# resolve globally, as consumers and as definition suppliers.
#
# Where a symbol has more than one co-resident definition the pick is the
# one still live once the whole composition has loaded.
#
import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path

from ap101Utils.addrcon import (
    RLD_BSR_ONLY, RLD_DSR_ONLY, RLD_ZCON_ADDR, RLD_ZCON_CODE, RLD_ZCON_DATA,
    AddrCon, ZCon, sector_decode,
)
from ap101Utils.addr import Addr, AddressMap
from ap101Utils.libModule import LibModule
from ap101Utils.objModule import EsdType

log = logging.getLogger("LNK101")

_ZCON_TYPES = (RLD_ZCON_CODE, RLD_ZCON_ADDR, RLD_ZCON_DATA)
_SECTOR_TYPES = _ZCON_TYPES + (RLD_BSR_ONLY, RLD_DSR_ONLY)


def phaseConfigs(deck):
    """phase number ->  memory-configuration names containing it"""
    from ap101Utils.mcconfigs import load_configs
    member: dict[int, set[str]] = {}
    for name, cfg in load_configs(deck).items():
        for p in cfg.phases:
            member.setdefault(p, set()).add(name)
    return {p: frozenset(s) for p, s in member.items()}


def configPhaseOrder(deck):
    """config name -> [phase numbers in LOAD order]"""
    from ap101Utils.mcconfigs import load_configs
    return {name: list(cfg.phases)
            for name, cfg in load_configs(deck).items()}


def restrictedNoCall(deck, phaseConfigsMap):
    """phase number -> set of symbols the deck's `LIBRARY *(er,...)`
    cards forbid this pass from resolving.

    RESTRICTED NO-CALL says the named external references are never
    resolved and the load module keeps the assembled text with no RLD
    fixup."""
    from ap101Utils.concard import library_no_call
    own = {}
    for p in phaseConfigsMap:
        try:
            own[p] = library_no_call(deck, f"PHASE{p:02d}")[0]
        except KeyError:            # phase with no deck member of its own
            own[p] = set()
    out = {}
    for p, mine in own.items():
        cp = phaseConfigsMap.get(p) or frozenset()
        syms = set(mine)
        for q, theirs in own.items():
            if q != p and cp and theirs and cp <= (phaseConfigsMap.get(q)
                                                  or frozenset()):
                syms |= theirs
        out[p] = frozenset(syms)
    return out


def displayLoadDefs(mmuRoot, deck):
    """Definitions for the SSW/OPS0 display-load staged csects, as
    {name: (definer-configs, absolute byte address)}."""
    from ap101Utils.mcconfigs import load_configs
    from tools.mmu2fcm import compose, iplMfbRegions
    configs = load_configs(deck)
    base = configs.get("SSW")       # the base composition, as in mmu2fcm
    if base is None:
        return {}
    phases = list(base.phases)
    iplCut = None
    names, _member = iplMfbRegions(deck, phases[-1])
    if names:
        iplCut = (phases[-1], names)
    mmuLog = logging.getLogger("mmu2fcm")
    level = mmuLog.level
    mmuLog.setLevel(logging.ERROR)  # loadPhase's not-yet-resolved warnings
    try:
        _, _, _, ops0 = compose(Path(mmuRoot), phases, iplCut=iplCut,
                                deck=deck)
    finally:
        mmuLog.setLevel(level)
    if not ops0:
        # legitimate on a deck with no IPL cut; also the SM phase's
        # lib/sym.json missing (ops0DisplayLoad warns, suppressed above)
        log.info("no display-load staging found; resolving from the "
                 "phase libs alone")
        return {}
    out = {n: (frozenset({"SSW"}), 2 * a)
           for n, (a, _sz) in ops0["sections"].items()}
    log.info("display-load definitions: " + ", ".join(
        f"{n}@0x{a // 2:05X}" for n, (_c, a) in sorted(out.items())))
    return out


def _phaseNumber(path, lib):
    """Phase number from the .lib file stem (con80build names them
    PHASEnn.lib) or the load module name; None when neither matches."""
    for cand in (Path(path).stem, (lib.name or "").strip()):
        if cand.upper().startswith("PHASE") and cand[5:].isdigit():
            return int(cand[5:])
    return None


def _inScope(consumerConfigs, definerConfigs):
    """May a site in a phase with `consumerConfigs` take a definition from a
    phase with `definerConfigs`?  None = in no configuration = global."""
    if consumerConfigs is None or definerConfigs is None:
        return True
    return bool(consumerConfigs & definerConfigs)


def _sectionMap(lib):
    """The lib's control sections as sorted (start, end, name) byte
    intervals."""
    out = [(e.address, e.address + max(int(e.length or 0), 1), e.name.strip())
           for e in lib.cesd
           if e.type == EsdType.SD and e.address is not None]
    out.sort()
    return out


def _sectionAt(secMap, addr):
    """Name of the section covering byte `addr` in one phase's map, or None."""
    for start, end, name in secMap:
        if start <= addr < end:
            return name
    return None


def _survivesLoad(phase, addr, phases, secMaps):
    """Is the csect that phase `phase` places over byte `addr` still the one
    there after every phase of a composition has loaded, in order?"""
    home = _sectionAt(secMaps.get(phase) or (), addr)
    if home is None:
        return False
    owner = None
    for p in phases:                       # load order: last writer wins
        nm = _sectionAt(secMaps.get(p) or (), addr)
        if nm is not None:
            owner = nm
    return owner == home


def _livePick(cand, consumerConfigs, configPhases, secMaps):
    """The definition address that survives the load in EVERY memory
    configuration the consuming phase belongs to, or None to leave the
    first-in-file-order pick alone."""
    if not consumerConfigs or not configPhases:
        return None                        # global scope: no composition
    inScope = [e for e in cand if _inScope(consumerConfigs, e[0])]
    if len({e[1] for e in inScope}) < 2:
        return None                        # nothing to choose between
    answers = set()
    for cfg in consumerConfigs:
        phases = configPhases.get(cfg)
        if phases is None:
            return None                    # unknown composition
        pick = None
        for configs, addr, phase in inScope:
            if phase is None:              # staged definition
                return None
            if phase in phases and _survivesLoad(phase, addr, phases,
                                                 secMaps):
                pick = addr
                break
        answers.add(pick)
    if len(answers) != 1:
        return None
    return answers.pop()                   # None when no config had one


def _mapConstant(cand):
    """A definition that two MUTUALLY EXCLUSIVE compositions both place at
    the SAME address, for a consumer in neither of them."""

    addrs = {addr for _c, addr, _p in cand}
    if len(addrs) != 1:
        return None                        # variants disagree: not a slot
    scopes = [c for c, _a, _p in cand]
    if any(c is None for c in scopes):
        return None                        # a global phase is in scope anyway
    for i, a in enumerate(scopes):
        for b in scopes[i + 1:]:
            if not (a & b):                # two compositions, one address
                return next(iter(addrs))
    return None


class PhaseResolution:
    """Result of one resolvePhases run."""

    def __init__(self):
        self.patched = {}       # lib name -> patch count
        self.remaining = {}     # lib name -> {symbol: site count}
        self.restricted = {}    # lib name -> {symbol: site count} left
        #                         unresolved on purpose (LIBRARY *(...))
        self.conflicts = []     # (symbol, [(libName, addr), ...])
        self.errors = []
        self.recorded = {}      # lib name -> sym.json entries added

    @property
    def totalPatched(self):
        return sum(self.patched.values())

    @property
    def totalRecorded(self):
        return sum(self.recorded.values())

    @property
    def totalRemaining(self):
        return sum(sum(v.values()) for v in self.remaining.values())

    @property
    def totalRestricted(self):
        return sum(sum(v.values()) for v in self.restricted.values())


def _symbolDefs(libs, result):
    """name -> [(definer configs-or-None, absolute byte address, definer
    phase number-or-None), ...] over every lib's SD/LD entries, in phase
    file order, first definition per lib.
    A name defined at differing addresses across phases is a conflict
    (overlay variants of the same table land at the same address by MFB
    convention); the first in-scope definition (lowest phase file order)
    wins for patching, unless _livePick overrides it."""
    defs = {}
    where = {}
    first = {}
    for configs, phase, lib in libs:
        seen = set()
        for e in lib.cesd:
            if e.type not in (EsdType.SD, EsdType.LD) or e.address is None:
                continue
            name = e.name.strip()
            if name in first:
                if first[name] != e.address:
                    where.setdefault(name, []).append((lib.name, e.address))
            else:
                first[name] = e.address
                where[name] = [(lib.name, e.address)]
            if name not in seen:  # one definition per lib (first wins)
                seen.add(name)
                defs.setdefault(name, []).append((configs, e.address, phase))
    result.conflicts = [(n, w) for n, w in where.items() if len(w) > 1]
    return defs


def _lookup(defs, name, consumerConfigs, configPhases=None, secMaps=None):
    cand = defs.get(name, ())
    live = _livePick(cand, consumerConfigs, configPhases, secMaps or {})
    if live is not None:
        return live
    for definerConfigs, addr, _phase in cand:
        if _inScope(consumerConfigs, definerConfigs):
            return addr
    return _mapConstant(cand)


def _conLength(flags):
    """Byte width of the constant an RLD's flags describe (LL+1)."""
    return ((flags >> 2) & 0x03) + 1


@dataclass(frozen=True)
class RelocRecord:
    address: int            # site position, in halfwords
    target: int             # resolved value, read back off the patched bytes
    targetName: str         # the external reference the site named
    flags: int              # the RLD's flag byte
    symbol: str | None = None   # 'SECTION+off' for target; set by updateManifest

    @classmethod
    def fromSite(cls, name, position, flags, extent, offset, target):
        """The record for one patched site, read back off the bytes now in
        the extent, or None for a sector-only RLD: those write the BSR/DSR
        nibbles of a ZCON whose address halfword carries its own entry, and
        linker.py skips them for the same reason."""
        if flags & 0x7F in (RLD_BSR_ONLY, RLD_DSR_ONLY):
            return None
        con = AddrCon(flags, _conLength(flags))
        final = int.from_bytes(
            bytes(extent.data[offset:offset + con.length]), 'big')
        return cls(address=Addr(position).hw,
                   target=(sector_decode(final, target.sector)
                           if con.length == 2 else final),
                   targetName=name,
                   flags=flags)

    @property
    def key(self):
        return (self.address, self.targetName)

    def withSymbol(self, amap):
        """This record with 'symbol' named off `amap`, falling back to the
        ER name when the address lands outside every mapped range."""
        return replace(self, symbol=amap.format(self.target) or self.targetName)

    def asdict(self):
        d = {"address": self.address, "target": self.target,
             "targetName": self.targetName, "flags": self.flags}
        if self.symbol is not None:
            d["symbol"] = self.symbol
        return d


def _patchSite(extent, offset, flags, target):
    """Apply one relocation into extent.data at byte offset."""
    buf = extent.data if isinstance(extent.data, bytearray) \
        else bytearray(extent.data)
    flagType = flags & 0x7F
    length = _conLength(flags)
    if flagType in _SECTOR_TYPES:
        zcon = ZCon.from_image(buf, offset)
        if flagType in _ZCON_TYPES:
            # The link left unresolved address-ZCONs with bit 15 of HW0 set
            # (the real LE's behavior, issue #22); clear the marker so the
            # sector-encoding apply does not double-count it.
            zcon.hw0 &= 0x7FFF
        zcon.apply(target, flagType, flags)
        zcon.write_to_image(buf, offset)
    else:
        con = AddrCon(flags, length)
        existing = int.from_bytes(buf[offset:offset + con.length], 'big')
        newValue = con.apply(existing, target)
        buf[offset:offset + con.length] = newValue.to_bytes(con.length, 'big')
    extent.data = bytes(buf)


def defaultSymFor(libPath, lib):
    libPath = Path(libPath)
    for cand in (libPath.parent / libPath.stem / f"{libPath.stem}.sym.json",
                 libPath.with_suffix(".sym.json")):
        if cand.exists():
            return cand
    return None


def updateManifest(symPath, records):
    """Fold cross-phase-resolved sites (RelocRecords) into a phase manifest:
    add them to 'relocations' and drop them from 'unresolvedRelocations'.
    Idempotent -- a site the manifest already carries is left alone."""
    path = Path(symPath)
    data = json.loads(path.read_text())
    have = {(r["address"], r["targetName"])
            for r in data.get("relocations", ())}
    new = [r for r in records if r.key not in have]
    if not new:
        return 0
    # 'symbol' names the resolved value the way linker.py does, off the
    # phase's own sections and symbols rather than a live AddressMap.
    amap = AddressMap()
    for s in data.get("sections", ()):
        start = s.get("address")
        if start is not None:
            amap.add(start, start + max(int(s.get("size", 0)), 1) - 1,
                     s["name"])
    for s in data.get("symbols", ()):
        a = s.get("address")
        if a is not None and s.get("kind") != "section":
            amap.add(a, a, s["name"])
    new = [r.withSymbol(amap) for r in new]
    data["relocations"] = sorted(
        data.get("relocations", []) + [r.asdict() for r in new],
        key=lambda r: r["address"])
    drop = {r.key for r in new}
    if "unresolvedRelocations" in data:
        data["unresolvedRelocations"] = [
            u for u in data["unresolvedRelocations"]
            if (u.get("imageOffsetHW"),
                (u.get("symbol") or "").strip()) not in drop]
    path.write_text(json.dumps(data, indent=2))
    return len(new)


def resolvePhases(libPaths, fcmFor=None, dryRun=False, phaseConfigsMap=None,
                  symFor=None, repair=False, stagedDefs=None,
                  restrictedMap=None, configPhases=None):
    """Cross-resolve every phase .lib in `libPaths`, each against the union
    of the phases sharing at least one memory configuration with it.

    phaseConfigsMap: {phase number: frozenset of config names} (see
    phaseConfigs); None resolves every phase globally.
    fcmFor: optional callable (libPath, LibModule) -> fcm Path or None; when
    it yields a path, the same patches are written into the raw image.
    symFor: the same for the phase's <PHASE>.sym.json, whose relocation
    lists are brought in step with the image (see updateManifest).
    stagedDefs: {name: (definer-configs, absolute byte address)} extra
    definitions that take precedence over the libs' own (see
    displayLoadDefs); None adds nothing.
    restrictedMap: {phase number: frozenset of symbols} the deck's
    `LIBRARY *(er,...)` cards forbid resolving (see restrictedNoCall); a
    site naming one is left with its assembled text, gets no relocation
    record, and is counted in result.restricted rather than
    result.remaining.
    configPhases: {config name: [phase numbers in LOAD order]} 

    Returns a PhaseResolution.  Each patched .lib gets an XRES marker record;
    a lib already carrying one is refused."""
    result = PhaseResolution()
    libs = []       # patch targets: not yet cross-resolved
    defOnly = []    # already resolved: definition sources only, never
    #                 re-patched (their addends are consumed).  This is the
    #                 single-phase-relink workflow: re-linking one phase
    #                 regenerates just its lib; the sibling libs stay
    #                 XRES-marked and only feed the symbol table.
    allLibs = []    # every input in file order (defs keep first-wins order)
    for p in libPaths:
        lib = LibModule.read(p)
        allLibs.append((Path(p), lib))
        (defOnly if lib.crossResolved else libs).append((Path(p), lib))
    if repair:
        for p, lib in libs:
            result.errors.append(
                f"{p}: not cross-phase resolved; nothing to record "
                f"(resolve it without --repair-manifest first)")
        libs, defOnly = defOnly, []
    if not libs:
        # Nothing to patch: report every input as refused (the historic
        # double-resolution error).
        for p, _ in defOnly:
            result.errors.append(
                f"{p}: already cross-phase resolved (re-link the phase to "
                f"regenerate it before resolving again)")
        return result

    def configsOf(path, lib):
        if phaseConfigsMap is None:
            return None
        n = _phaseNumber(path, lib)
        return phaseConfigsMap.get(n) if n is not None else None

    defs = _symbolDefs([(configsOf(p, lib), _phaseNumber(p, lib), lib)
                        for p, lib in allLibs], result)
    for name, entry in (stagedDefs or {}).items():
        configs, addr = entry
        defs.setdefault(name, []).insert(0, (configs, addr, None))

    secMaps = {}
    if configPhases:
        for p, lib in allLibs:
            n = _phaseNumber(p, lib)
            if n is not None:
                secMaps[n] = _sectionMap(lib)

    for path, lib in libs:
        myConfigs = configsOf(path, lib)
        phaseNo = _phaseNumber(path, lib)
        restricted = (restrictedMap or {}).get(phaseNo) or frozenset()
        extents = sorted(lib.extents, key=lambda x: x.address)
        patched = 0
        remaining = {}
        noCall = {}
        records = []
        picks = {}        # symbol -> resolved address, per consuming phase

        for rld in lib.rlds:
            entry = lib.cesdEntry(rld.relId)
            if entry is None or entry.type != EsdType.ER:
                continue          # resolved at phase-link time
            name = entry.name.strip()
            if name in restricted:
                # RESTRICTED NO-CALL (LIBRARY *(name)): the site keeps the
                # assembled text with no fixup, whether or not a sibling
                # phase happens to define the symbol.
                noCall[name] = noCall.get(name, 0) + 1
                continue
            if name in picks:
                addr = picks[name]
            else:
                addr = picks[name] = _lookup(defs, name, myConfigs,
                                             configPhases, secMaps)
            if addr is None:
                remaining[name] = remaining.get(name, 0) + 1
                continue
            extent = next((x for x in extents
                           if x.address <= rld.position < x.address
                           + len(x.data)), None)
            if extent is None:
                result.errors.append(
                    f"{lib.name}: RLD site 0x{rld.position:06X} ({name}) "
                    f"outside every TEXT extent")
                continue
            offset = rld.position - extent.address
            if not repair:
                _patchSite(extent, offset, rld.flags, Addr(addr))
            patched += 1
            rec = RelocRecord.fromSite(name, rld.position, rld.flags,
                                       extent, offset, Addr(addr))
            if rec is not None:
                records.append(rec)

        result.patched[lib.name] = patched
        result.remaining[lib.name] = remaining
        result.restricted[lib.name] = noCall
        if noCall:
            log.info(f"{lib.name}: {sum(noCall.values())} site(s) left "
                     f"unresolved by LIBRARY *(...) restricted no-call: "
                     + ", ".join(f"{s}({n})" for s, n in sorted(noCall.items())))

        sym = symFor(path, lib) if symFor else None
        if sym is not None and records and not dryRun:
            result.recorded[lib.name] = updateManifest(sym, records)
            log.info(f"recorded {result.recorded[lib.name]} relocation(s) "
                     f"in {sym}")
        
        if dryRun or repair or (patched == 0 and not noCall):
            continue
        lib.crossResolved = True
        lib.xresCount = patched
        lib.write(path)
        fcm = fcmFor(path, lib) if fcmFor else None
        if fcm is not None and Path(fcm).exists():
            image = bytearray(Path(fcm).read_bytes())
            for x in sorted(lib.extents, key=lambda e: e.address):
                end = x.address + len(x.data)
                if end <= len(image):
                    image[x.address:end] = x.data
                else:
                    result.errors.append(
                        f"{fcm}: extent 0x{x.address:06X}..0x{end:06X} "
                        f"beyond image end 0x{len(image):06X}; not patched")
            Path(fcm).write_bytes(image)
            log.info(f"patched {fcm}")

    return result


def defaultFcmFor(libPath, lib):
    """Locate the .fcm that pairs a phase .lib in the con80build layouts:
    <dir>/<NAME>/<NAME>.fcm (phase mode) or <dir>/<NAME>.fcm (legacy)."""
    libPath = Path(libPath)
    for cand in (libPath.parent / libPath.stem / f"{libPath.stem}.fcm",
                 libPath.with_suffix(".fcm")):
        if cand.exists():
            return cand
    return None


def main(argv=None):
    import argparse

    from ap101Utils.concard import ConcardDeck
    from ap101Utils.mcconfigs import McConfigError

    ap = argparse.ArgumentParser(
        prog="python -m lnk101.phaseresolve",
        description="Cross-resolve phase .lib load modules against each "
                    "other, patching residual RLD sites in the .lib extents "
                    "and paired .fcm images.")
    ap.add_argument("libs", nargs="+", help="phase .lib files")
    ap.add_argument("--con80", type=Path, required=True,
                    help="CON80 deck directory (memory-configuration "
                         "registry scoping the resolution)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be patched; write nothing")
    ap.add_argument("--no-fcm", action="store_true",
                    help="patch only the .lib files, not the paired .fcm")
    ap.add_argument("--no-sym", action="store_true",
                    help="do not fold the resolved sites into the paired "
                         "<PHASE>.sym.json relocation lists")
    ap.add_argument("--repair-manifest", action="store_true",
                    help="rebuild the <PHASE>.sym.json relocation lists from "
                         "an already-resolved tree; patches nothing and "
                         "writes no .lib or .fcm")
    ap.add_argument("--Wunresolved-phases", dest="warn_unresolved",
                    action="store_true",
                    help="list every symbol still unresolved after "
                    "cross-phase resolution (default: counts only)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="LNK101: %(message)s")
    try:
        deck = ConcardDeck(args.con80)
        configsMap = phaseConfigs(deck)
        loadOrder = configPhaseOrder(deck)
        restricted = restrictedNoCall(deck, configsMap)
    except (NotADirectoryError, McConfigError) as e:
        ap.error(str(e))
    try:
        staged = displayLoadDefs(Path(args.libs[0]).parent, deck)
    except (Exception, SystemExit) as e:      # compose SystemExits on a bad lib
        log.warning(f"display-load definitions unavailable ({e}); "
                    f"resolving from the phase libs alone")
        staged = None
    res = resolvePhases(args.libs,
                        fcmFor=None if args.no_fcm else defaultFcmFor,
                        dryRun=args.dry_run,
                        phaseConfigsMap=configsMap,
                        symFor=None if args.no_sym else defaultSymFor,
                        repair=args.repair_manifest,
                        stagedDefs=staged,
                        restrictedMap=restricted,
                        configPhases=loadOrder)
    for e in res.errors:
        print(f"LNK101: ERROR: {e}")
    for name, sites in res.conflicts:
        locs = ", ".join(f"{ln}@0x{a:06X}" for ln, a in sites)
        print(f"LNK101: warning: '{name}' defined at differing addresses: "
              f"{locs} (first wins)")
    for libName in res.patched:
        rem = res.remaining.get(libName, {})
        nRem = sum(rem.values())
        noCall = res.restricted.get(libName, {})
        rec = res.recorded.get(libName)
        verb = "recorded" if args.repair_manifest else "resolved"
        print(f"{libName}: {res.patched[libName]} site(s) {verb}"
              f"{' (dry run)' if args.dry_run else ''}"
              f"{'' if rec is None else f', {rec} added to the manifest'}, "
              f"{len(rem)} symbol(s)/{nRem} site(s) still unresolved"
              + (f", {sum(noCall.values())} site(s) no-called by "
                 f"LIBRARY *(...)" if noCall else ""))
        if args.warn_unresolved:
            for sym in sorted(rem):
                print(f"  unresolved: {sym} ({rem[sym]} site(s))")
            for sym in sorted(noCall):
                print(f"  restricted no-call: {sym} ({noCall[sym]} site(s))")
    return 1 if res.errors else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
