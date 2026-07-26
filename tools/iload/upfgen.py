#!/usr/bin/env python3
"""upfgen -- harvest STS-134 I-load values from MAFGEN DASS listings and emit
them as a Universal Patch Format (UPF) card deck, the format of the I-load
tapes JSC shipped to KSC (KSC-LPS-IB-070-10-2 sec 8.2.3.2.2.8: "THE I-LOAD AND
MASS MEMORY PATCH TAPES RECEIVED FROM JSC WILL BE IN THE SAME FORMAT ... CARD
IMAGE UNIVERSAL PATCH FORMAT (UPF)").

Pipeline:
  1. PSFIN catalog: every V-card in OI340600.PSFIN carries D ILD; -- the set
     of base HAL names below is exactly the I-load population.
  2. DASS scrape: the DASS memory map annotates data csects per variable;
     '*'-prefixed halfwords are memory that differs from the load module,
     i.e. the cells reconfiguration (I-load application) changed.
  3. Address mapping: csect -> phase via build/mmu/PHASEnn sym.json;
     load block = TEXT extent ordinal within the phase .lib (mmu2fcm's
     model: "our load blocks ARE the .lib TEXT extents"); offset = halfword
     offset within the extent; SP bit from the extent protect bitmap.
  4. Emit A/B/C/D/E cards per KSC-LPS-IB-070-10-2 sec 8.2.3.2.2.8/9, as an
     ASCII deck and an EBCDIC 20-cards-per-1600-byte-block tape image.
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from ap101Utils.libModule import LibModule  # noqa: E402

MM_VER = "29.01"        # from the flight image PSA system id 29.01A.01B
FSW_SYSTEM = 1          # decimal 0-7; not recoverable from the image (see README)
CARD_COLS = 80

# ---------------------------------------------------------------- PSFIN

def psfinCatalog(psfinDir: Path):
    """Base HAL name -> list of (fullName, msid) from every PSFIN V-card."""
    cat = defaultdict(list)
    for member in sorted(psfinDir.iterdir()):
        cur, attrs = None, ""
        def flush():
            if cur is None:
                return
            base = cur.split("$")[0].strip()
            m = re.search(r"MSID=\s*(\S+?)\s*;", attrs)
            msid = m.group(1) if m else None
            cat[base].append((cur, msid))
            if "." in base:
                # structure-qualified card (STRUCT.TERMINAL); the DASS
                # annotates the bare terminal name
                cat[base.split(".")[-1]].append((cur, msid))
        for ln in member.read_text(encoding="latin-1").splitlines():
            t = ln[:1]
            if t == "V":
                flush(); cur = ln[1:72].strip(); attrs = ""
            elif t == "D" and cur is not None:
                attrs += " " + ln[1:72]
            elif t in ("U", "B"):
                flush(); cur = None
        flush()
    return cat

# ---------------------------------------------------------------- DASS scrape

HDR_RE = re.compile(
    r"^\s?([0-9A-F]{6})(?:-([0-9A-F]{6}))?\s+(#\S+)\+([0-9A-F]{4})\s+(.*)$")
TOK_RE = re.compile(r"(\*?)\b([0-9A-F]{4})\b")
TYPE_WORDS = ("VARIABLE", "TERMINAL", "STRUCTURE")


def scrapeDass(path: Path):
    """Return ([(hwAddr, csect, csectOff, varName, value, starred)], mcfPhases)
    from the DASS memory map.  mcfPhases comes from the listing's own
    "CONFIGURATION x IS DEFINED IN THE MCF AS PHASES n,n,.." line."""
    rows = []
    curName, curEnd = None, -1
    mcf = None
    for ln in path.read_text(encoding="latin-1", errors="replace").splitlines():
        if mcf is None and "DEFINED IN THE MCF AS PHASES" in ln:
            mcf = [int(x) for x in re.findall(r"[\d,]+\s*$", ln)[0].split(",")]
        m = HDR_RE.match(ln)
        if not m:
            continue
        lo = int(m.group(1), 16)
        hi = int(m.group(2), 16) if m.group(2) else lo
        csect, rest = m.group(3), m.group(5)
        isHeader = any(w in rest for w in TYPE_WORDS)
        if isHeader:
            curName, curEnd = rest.split()[0], hi
            rest = rest[len(curName):]
            # strip the trailing type keywords so decoded EU values / type
            # words can't contribute stray hex tokens
            for w in TYPE_WORDS:
                idx = rest.find(w)
                if idx >= 0:
                    rest = rest[:idx]
        elif curName is None or lo > curEnd:
            continue
        need = hi - lo + 1
        csOff = int(m.group(4), 16)
        toks = TOK_RE.findall(rest)[:need]
        # inline scalar headers carry their halfwords on the header line;
        # vector/array content arrives on continuation lines with the same
        # csect+offset addressing.  Decoded-EU columns never match: they are
        # floats like 1.8958997E-01 whose digit runs exceed 4 chars.
        if len(toks) == need:
            for i, (star, val) in enumerate(toks):
                rows.append((lo + i, csect, csOff + i, curName,
                             int(val, 16), star == "*"))
    return rows, mcf


# ---------------------------------------------------------------- phase map

def phaseExtents(mmuRoot: Path, phases):
    """csect -> (phase, lbIndex, extStartHw, protect) for every csect in the
    given phases.  Load block = 1-based TEXT extent ordinal in the .lib."""
    out = {}
    for n in phases:
        name = f"PHASE{n:02d}"
        libPath = mmuRoot / f"{name}.lib"
        symPath = mmuRoot / name / f"{name}.sym.json"
        if not libPath.exists() or not symPath.exists():
            continue
        lib = LibModule.read(libPath)
        exts = [(i + 1, x.address // 2, x.address // 2 + x.hwLength, x.protect)
                for i, x in enumerate(lib.extents)]
        sym = json.loads(symPath.read_text())
        for sec in sym["sections"]:
            hw = sec["address"]          # sym.json addresses are halfword
            for lb, lo, hi, prot in exts:
                if lo <= hw < hi:
                    out.setdefault(sec["name"], []).append(
                        (n, lb, lo, prot, hw))
                    break
    return out


# ---------------------------------------------------------------- UPF emit

def card(text_fields):
    """Build one 80-col card from [(col1based, text)]."""
    buf = [" "] * CARD_COLS
    for col, text in text_fields:
        for i, ch in enumerate(text):
            buf[col - 1 + i] = ch
    return "".join(buf).rstrip().ljust(CARD_COLS)


def emitDeck(groups, comment):
    """groups: list of ((phase, lb), [ (offsetHw, addrHw, sp, [words]) ]).
    One A/B/C header per (phase,lb) group, D/E per run, per the spec:
    'IF LOAD BLOCK ... OR PATCH ID IS DIFFERENT, THE SEQUENCE IS STARTED
    FRESH WITH THE CARD A.'"""
    cards, patchSeq = [], 0
    for (phase, lb), runs in groups:
        patchSeq += 1
        num = 1
        cards.append(card([(1, "BEGIN PATCH"), (20, comment[:44]),
                           (65, f"{num:05d}"), (71, "A")]))
        num += 1
        cards.append(card([(1, "PATCH ID"), (12, f"{0x134000 + patchSeq:06X}"),
                           (20, "STS-134 ILOAD SCRAPE"),
                           (65, f"{num:05d}"), (71, "B")]))
        num += 1
        cards.append(card([(1, "FSW SYSTEM"), (12, str(FSW_SYSTEM)),
                           (20, "MM VER"), (27, MM_VER),
                           (36, "PHASE,LB"), (46, f"{phase:03d}"), (49, ","),
                           (50, f"{lb:03d}"),
                           (65, f"{num:05d}"), (71, "C")]))
        for offset, addr, sp, words in runs:
            num += 1
            cards.append(card([(1, "OFFSET"), (12, f"{offset:05d}"),
                               (20, "NUMBER"), (27, f"{len(words):03d}"),
                               (36, "AP101 ADDR"), (47, f"{addr:05X}"),
                               (55, "SP"), (58, str(sp)),
                               (65, f"{num:05d}"), (71, "D")]))
            for i in range(0, len(words), 5):
                num += 1
                fields = [(1, "CONTENTS")]
                for j, w in enumerate(words[i:i + 5]):
                    fields.append((12 + 7 * j, f"{w:04X}"))
                fields += [(65, f"{num:05d}"), (71, "E")]
                cards.append(card(fields))
    return cards


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pfs", type=Path, required=True,
                    help="pfs tree root (contains code/OI340600, PFS/mafgen)")
    ap.add_argument("--mmu", type=Path, required=True,
                    help="con80build output root (build/mmu)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--configs", nargs="*", default=None,
                    help="DASS config names (default: every DASS_*.ASC)")
    a = ap.parse_args()

    psfin = psfinCatalog(a.pfs / "code" / "OI340600" / "PSFIN")
    iloadNames = set(psfin)
    print(f"PSFIN catalog: {sum(len(v) for v in psfin.values())} I-loads, "
          f"{len(iloadNames)} base HAL names")

    dassDir = a.pfs / "PFS" / "mafgen"
    dassFiles = sorted(dassDir.glob("DASS_*.ASC"))
    if a.configs:
        dassFiles = [f for f in dassFiles
                     if any(c in f.name for c in a.configs)]

    # perCfg[cfg] = ([(hw, csect, csOff, var, val)], mcfPhases) starred+ILD
    perCfg, harvest = {}, defaultdict(dict)
    for f in dassFiles:
        cfg = f.stem.replace("DASS_", "").replace("_(PostIPL)", "")
        rows, mcf = scrapeDass(f)
        keep = []
        for hw, csect, csOff, var, val, star in rows:
            base = var.split("$")[0]
            if base not in iloadNames:
                continue
            harvest[f"{csect}:{var}"].setdefault("cells", {})[f"+{csOff:04X}"] = \
                {"value": f"{val:04X}", "flightAddr": f"{hw:06X}",
                 "reconfigured": star, "config": cfg}
            if star:
                keep.append((hw, csect, csOff, var, val))
        perCfg[cfg] = (keep, mcf)
        print(f"{f.name}: {len(keep)} reconfigured I-load halfwords, "
              f"MCF phases {mcf}")

    a.out.mkdir(parents=True, exist_ok=True)
    phases = sorted(int(p.name[5:7]) for p in a.mmu.glob("PHASE??.lib"))
    csectMap = phaseExtents(a.mmu, phases)

    # resolve each cell to a phase within its OWN config's MCF phase set --
    # per-MC copies of a same-named csect land in different phases and must
    # not be merged.  Dedupe on (phase, csect, csectOff) afterwards.
    cells, conflicts, unmapped = {}, [], {}
    homeOf = {}
    for cfg, (keep, mcf) in perCfg.items():
        for hw, csect, csOff, var, val in keep:
            homes = csectMap.get(csect, [])
            cand = [h for h in homes if mcf is None or h[0] in mcf]
            if not cand:
                unmapped.setdefault(csect, []).append(
                    {"config": cfg, "csectOffset": f"+{csOff:04X}",
                     "flightAddr": f"{hw:06X}", "value": f"{val:04X}"})
                continue
            home = cand[-1]        # last-loaded phase wins (overlay order)
            key = (home[0], csect, csOff)
            if key in cells and cells[key][0] != val:
                conflicts.append((key, cells[key][0], val, cfg))
            cells[key] = (val, hw, var, cfg)
            homeOf[(home[0], csect)] = home
    if conflicts:
        print(f"WARNING: {len(conflicts)} value conflicts AFTER per-config "
              f"phase resolution: {conflicts[:4]}")

    # merge contiguous csect-relative cells into runs (max 512 words each)
    byCsect = defaultdict(list)
    for (phase, csect, csOff), (val, fhw, var, cfg) in sorted(cells.items()):
        byCsect[(phase, csect)].append((csOff, val, fhw))
    groups = defaultdict(list)
    for (phase, csect), offs in sorted(byCsect.items()):
        phase, lb, extLo, prot, secHw = homeOf[(phase, csect)]
        run = []
        def flushRun():
            if not run:
                return
            startOff = secHw + run[0][0] - extLo   # hw offset in load block
            pi = secHw + run[0][0] - extLo
            sp = 1 if prot and 0 <= pi < len(prot) and prot[pi] else 0
            groups[(phase, lb)].append(
                (startOff, run[0][2], sp, [v for _, v, _ in run]))
        for csOff, val, fhw in offs:
            if run and (csOff != run[-1][0] + 1 or len(run) >= 512):
                flushRun(); run = []
            run.append((csOff, val, fhw))
        flushRun()

    if unmapped:
        nhw = sum(len(v) for v in unmapped.values())
        print(f"WARNING: {len(unmapped)} csects ({nhw} hw) not in any built "
              f"phase -- written to unmapped.json, excluded from deck: "
              f"{list(unmapped)[:8]}")
        (a.out / "unmapped.json").write_text(json.dumps(unmapped, indent=1))

    for runs in groups.values():
        runs.sort()
    ordered = sorted(groups.items())
    cards = emitDeck(ordered, "STS134/OI034/C2 ILOAD SCRAPE FROM DASS")
    a.out.mkdir(parents=True, exist_ok=True)
    deck = a.out / "STS134_ILOAD.upf"
    deck.write_text("\n".join(cards) + "\n")
    # 9-track image: EBCDIC card images, 20 cards (800 16-bit words) per block
    ebc = b"".join(c.encode("cp037") for c in cards)
    (a.out / "STS134_ILOAD.ebcdic").write_bytes(ebc)
    (a.out / "ids_sts134.json").write_text(json.dumps({
        "source": [f.name for f in dassFiles],
        "flight": "STS-134 / OI-34 / cycle 2 (DASS header: STS134/OI034/C2 "
                  "MDD 134.09)",
        "psfin": "OI340600.PSFIN",
        "variables": harvest}, indent=1))
    nRuns = sum(len(r) for r in groups.values())
    nHw = sum(len(w) for r in groups.values() for *_, w in r)
    print(f"wrote {deck}: {len(cards)} cards, {len(ordered)} patch groups, "
          f"{nRuns} runs, {nHw} halfwords in deck "
          f"({len(cells)} scraped)")


if __name__ == "__main__":
    main()
