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

Output is a phase-dir-shaped pair <out>/<NAME>.fcm + <NAME>.sym.json (the
union of the constituent phases' sym.json, overlay-aware).
"""
import argparse
import json
import logging
import sys
from pathlib import Path

from ap101Utils.concard import ConcardDeck
from ap101Utils.libModule import LibModule
from ap101Utils.mcconfigs import (CONFIG_NAMES, McConfigError,
                                  listConfigs, load_configs)
from ap101Utils.objModule import EsdType
from ap101Utils import psaIdent

log = logging.getLogger("mmu2fcm")

MEM_HW = 0x80000                     # 512K halfwords of addressable memory
FILL_LOW, FILL_HIGH = b"\xC9\xFB", b"\xC6\xC6"
FILL_SPLIT_HW = 0x20000              # C9FB below, C6C6 at/above


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


def compose(mmuRoot: Path, phases: list[int]):
    """IPL fill + overlay each phase's TEXT extents in load order.
    Returns (image bytes, owner bytearray, per-phase info list).
    owner[hw] = 0 for background fill, else 1-based index into phases."""
    image = bytearray(FILL_LOW * FILL_SPLIT_HW +
                      FILL_HIGH * (MEM_HW - FILL_SPLIT_HW))
    owner = bytearray(MEM_HW)
    info = []
    for idx, n in enumerate(phases, start=1):
        name, lib, sym = loadPhase(mmuRoot, n)
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
                         placedHW=placed, overlaidHW=overlaid))
        log.info(f"{name}: {len(lib.extents)} extents, {placed} hw placed"
                 + (f", {overlaid} hw overlay earlier phases" if overlaid
                    else ""))
    return bytes(image), owner, info


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


def unionSym(cfgName: str, owner: bytearray, info: list[dict]) -> dict:
    """Merge the constituent phases' sym.json overlay-aware:
    - sections/symbols: entries whose module carries text (no '>' marker)
      beat imported markers; among text-bearing duplicates the later-loaded
      phase wins (overlay semantics).
    - relocations / unresolvedRelocations: kept only if the fixup site still
      belongs to that phase in the composed image (not overlaid later).
    - modules: union by name."""
    def imported(entry):
        return entry.get("module", "").endswith(">")

    def mergeNamed(key):
        out = {}
        for ph in info:
            for e in ph["sym"].get(key, []):
                cur = out.get(e["name"])
                if cur is None or imported(cur[1]) or not imported(e):
                    out[e["name"]] = (ph["index"], e)
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
            "phases": [ph["phase"] for ph in info],
            "constituents": {ph["name"]: ph["sym"].get("repro", {})
                             for ph in info},
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

    if args.list_configs or not args.phases:
        try:
            configs = load_configs(ConcardDeck(args.con80))
        except (NotADirectoryError, McConfigError) as e:
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

    out = args.out or (args.mmu / args.config)
    out.mkdir(parents=True, exist_ok=True)

    image, owner, info = compose(args.mmu, phases)
    damaged = reportOverlays(image, owner, info)
    if damaged:
        log.warning(f"{damaged} cross-phase csect collisions (see above) -- "
                    f"composed bytes there are the later phase's text")
    sym = unionSym(args.config, owner, info)
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
