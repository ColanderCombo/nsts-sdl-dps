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
import logging
from pathlib import Path

from ap101Utils.addrcon import (
    RLD_BSR_ONLY, RLD_DSR_ONLY, RLD_ZCON_ADDR, RLD_ZCON_CODE, RLD_ZCON_DATA,
    AddrCon, ZCon,
)
from ap101Utils.addr import Addr
from ap101Utils.libModule import LibModule
from ap101Utils.objModule import EsdType

log = logging.getLogger('lnk101')

_ZCON_TYPES = (RLD_ZCON_CODE, RLD_ZCON_ADDR, RLD_ZCON_DATA)
_SECTOR_TYPES = _ZCON_TYPES + (RLD_BSR_ONLY, RLD_DSR_ONLY)


def phaseConfigs(deck):
    """phase number -> frozenset of memory-configuration names containing
    it, inverted from the deck-driven registry (ap101Utils.mcconfigs)."""
    from ap101Utils.mcconfigs import load_configs
    member: dict[int, set[str]] = {}
    for name, cfg in load_configs(deck).items():
        for p in cfg.phases:
            member.setdefault(p, set()).add(name)
    return {p: frozenset(s) for p, s in member.items()}


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


class PhaseResolution:
    """Result of one resolvePhases run."""

    def __init__(self):
        self.patched = {}       # lib name -> patch count
        self.remaining = {}     # lib name -> {symbol: site count}
        self.conflicts = []     # (symbol, [(libName, addr), ...])
        self.errors = []

    @property
    def totalPatched(self):
        return sum(self.patched.values())

    @property
    def totalRemaining(self):
        return sum(sum(v.values()) for v in self.remaining.values())


def _symbolDefs(libs, result):
    """name -> [(definer configs-or-None, absolute byte address), ...] over
    every lib's SD/LD entries, in phase file order, first definition per lib.
    A name defined at DIFFERING addresses across phases is a conflict
    (overlay variants of the same table land at the same address by MFB
    convention); the first IN-SCOPE definition (lowest phase file order)
    wins for patching."""
    defs = {}
    where = {}
    first = {}
    for configs, lib in libs:
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
                defs.setdefault(name, []).append((configs, e.address))
    result.conflicts = [(n, w) for n, w in where.items() if len(w) > 1]
    return defs


def _lookup(defs, name, consumerConfigs):
    """First in-scope definition address for `name`, or None."""
    for definerConfigs, addr in defs.get(name, ()):
        if _inScope(consumerConfigs, definerConfigs):
            return addr
    return None


def _patchSite(extent, offset, flags, target):
    """Apply one relocation into extent.data at byte offset."""
    buf = extent.data if isinstance(extent.data, bytearray) \
        else bytearray(extent.data)
    flagType = flags & 0x7F
    length = ((flags >> 2) & 0x03) + 1
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


def resolvePhases(libPaths, fcmFor=None, dryRun=False, phaseConfigsMap=None):
    """Cross-resolve every phase .lib in `libPaths`, each against the union
    of the phases sharing at least one memory configuration with it.

    phaseConfigsMap: {phase number: frozenset of config names} (see
    phaseConfigs); None resolves every phase globally.
    fcmFor: optional callable (libPath, LibModule) -> fcm Path or None; when
    it yields a path, the same patches are written into the raw image.
    Returns a PhaseResolution.  Each patched .lib gets an XRES marker record;
    a lib already carrying one is refused (its addends are consumed --
    re-link the phase to redo resolution)."""
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

    defs = _symbolDefs([(configsOf(p, lib), lib)
                        for p, lib in allLibs], result)

    for path, lib in libs:
        myConfigs = configsOf(path, lib)
        extents = sorted(lib.extents, key=lambda x: x.address)
        patched = 0
        remaining = {}

        for rld in lib.rlds:
            entry = lib.cesdEntry(rld.relId)
            if entry is None or entry.type != EsdType.ER:
                continue          # resolved at phase-link time
            name = entry.name.strip()
            addr = _lookup(defs, name, myConfigs)
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
            _patchSite(extent, offset, rld.flags, Addr(addr))
            patched += 1

        result.patched[lib.name] = patched
        result.remaining[lib.name] = remaining

        if dryRun or patched == 0:
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
    ap.add_argument("--Wunresolved-phases", dest="warn_unresolved",
                    action="store_true",
                    help="list every symbol still unresolved after "
                    "cross-phase resolution (default: counts only)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="LNK101S: %(message)s")
    try:
        configsMap = phaseConfigs(ConcardDeck(args.con80))
    except (NotADirectoryError, McConfigError) as e:
        ap.error(str(e))
    res = resolvePhases(args.libs,
                        fcmFor=None if args.no_fcm else defaultFcmFor,
                        dryRun=args.dry_run,
                        phaseConfigsMap=configsMap)
    for e in res.errors:
        print(f"LNK101S: ERROR: {e}")
    for name, sites in res.conflicts:
        locs = ", ".join(f"{ln}@0x{a:06X}" for ln, a in sites)
        print(f"LNK101S: warning: '{name}' defined at differing addresses: "
              f"{locs} (first wins)")
    for libName in res.patched:
        rem = res.remaining.get(libName, {})
        nRem = sum(rem.values())
        print(f"{libName}: {res.patched[libName]} site(s) resolved"
              f"{' (dry run)' if args.dry_run else ''}, "
              f"{len(rem)} symbol(s)/{nRem} site(s) still unresolved")
        if args.warn_unresolved:
            for sym in sorted(rem):
                print(f"  unresolved: {sym} ({rem[sym]} site(s))")
    return 1 if res.errors else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
